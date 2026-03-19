#!/usr/bin/env python3
"""
extract_data_v2.py  – Extended data extraction for the hybrid static + LLM pipeline.

Extends extract_data.py with Option A from NEW-DESIGN.md §4.2:
  For each changed file path in a PR, fetch the full file content at head_sha
  via the GitHub Contents API (no repo clone needed).

Adds ``full_files`` to each record:
  {
    "project_id": "E2541",
    "diff":             "...",    # existing
    "wiki_content":     "...",    # existing
    "project_feedback": "...",    # existing
    "design_feedback":  "...",    # existing
    "full_files": [               # NEW
        {"path": "app/controllers/foo.rb", "content": "..."},
        ...
    ]
  }

Only Ruby (.rb) and TypeScript (.ts, .tsx) files are fetched
(the static analyzer only handles those two languages).

Usage:
    python extract_data_v2.py --input projects.csv --output dataset_v2.jsonl
    python extract_data_v2.py --from-cache --input projects.csv --output dataset-2023.jsonl --year 2023

With --from-cache: builds entirely from pr_cache/ (no GitHub API, instant).
Each record includes semester, season, year parsed from projects.csv first column.

GitHub rate limits:
    Unauthenticated: 60 req/hour.  Set GITHUB_TOKEN or GH_TOKEN for 5000/hour.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Union

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants (kept in sync with extract_data.py)
# ─────────────────────────────────────────────────────────────────────────────
GITHUB_PR_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:/.*)?",
    re.IGNORECASE,
)
WIKI_PATTERN = re.compile(
    r"https?://(?:www\.)?wiki\.expertiza\.ncsu\.edu/[^\s)\]\"]+",
    re.IGNORECASE,
)

REQUEST_TIMEOUT   = 60  # Large diffs can take longer
MAX_CONCURRENT    = 10  # Increase when using multiple tokens / high rate limit
MIN_REQUEST_GAP   = 0   # No delay by default (use --delay if hitting secondary limits)
MAX_RETRIES       = 3   # Retries on 403 with backoff
USER_AGENT        = "Mozilla/5.0 (compatible; ThesisPRAnalyzer/2.0)"
GITHUB_API_BASE   = "https://api.github.com"
GITHUB_DIFF_ACCEPT = "application/vnd.github.v3.diff"
GITHUB_JSON_ACCEPT = "application/vnd.github.v3+json"

# File extensions to fetch full content for (static analysis targets)
STATIC_ANALYSIS_EXTS = {".rb", ".ts", ".tsx"}

# Max content size to fetch (safety limit: ~500 KB per file)
MAX_FILE_BYTES = 500_000


# ─────────────────────────────────────────────────────────────────────────────
# URL helpers (copied from extract_data.py for independence)
# ─────────────────────────────────────────────────────────────────────────────
def parse_github_pr_urls(links_text: str) -> list[tuple[str, str, str]]:
    """Extract (owner, repo, pr_number) tuples from a links string."""
    if pd.isna(links_text) or not str(links_text).strip():
        return []
    seen = set()
    results = []
    for m in GITHUB_PR_PATTERN.finditer(str(links_text)):
        owner, repo, pr_num = m.groups()
        key = (owner, repo, pr_num)
        if key not in seen:
            seen.add(key)
            results.append(key)
    return results


def parse_pr_urls_and_repo(links_text: str) -> tuple[list[str], str]:
    """
    Extract GitHub PR URLs and repo name from links text.
    Returns (pr_urls, repo_name).
    - pr_urls: list of full github.com/.../pull/... URLs, deduplicated
    - repo_name: repository name from first PR URL (e.g. expertiza, expertiza-reimplementation-backend)
    """
    pr_specs = parse_github_pr_urls(links_text)
    seen: set[str] = set()
    urls: list[str] = []
    repo_name = ""
    for owner, repo, pr_num in pr_specs:
        url = f"https://github.com/{owner}/{repo}/pull/{pr_num}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
        if not repo_name:
            repo_name = repo
    return (urls, repo_name)


def extract_wiki_urls(links_text: str) -> list[str]:
    if pd.isna(links_text) or not str(links_text).strip():
        return []
    urls, seen = [], set()
    for m in WIKI_PATTERN.finditer(str(links_text)):
        url = m.group(0).rstrip(".,;:)")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_main_content(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for selector in ["#mw-content-text", ".mw-parser-output", "main", "article", "#content", "body"]:
        if selector.startswith("#"):
            content = soup.find(id=selector[1:])
        elif selector.startswith("."):
            content = soup.find(class_=selector[1:])
        else:
            content = soup.find(selector)
        if content:
            text = content.get_text(separator="\n", strip=True)
            break
    else:
        text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API helpers (throttled + retry to avoid secondary rate limits)
# ─────────────────────────────────────────────────────────────────────────────
def _make_github_headers(token: Optional[str] = None) -> dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Accept": GITHUB_JSON_ACCEPT}
    tok = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


# Shared lock + timestamp for global request throttling (created in main_async to avoid "different loop" error)
_rate_limit_lock: Optional[asyncio.Lock] = None
_last_github_request = 0.0
_throttle_config = {"min_gap": MIN_REQUEST_GAP}


async def _throttle_github() -> None:
    """Ensure min_gap seconds between GitHub API requests."""
    global _last_github_request
    if _rate_limit_lock is None:
        return  # Not yet initialized (should not happen)
    min_gap = _throttle_config.get("min_gap", MIN_REQUEST_GAP)
    async with _rate_limit_lock:
        now = time.monotonic()
        gap = now - _last_github_request
        if gap < min_gap:
            await asyncio.sleep(min_gap - gap)
        _last_github_request = time.monotonic()


def _parse_403_body(body: bytes) -> str:
    """Extract GitHub's error message from 403 response body."""
    try:
        err = json.loads(body.decode("utf-8", errors="replace"))
        return err.get("message", err.get("documentation_url", str(err))[:200])
    except Exception:
        return (body[:200].decode("utf-8", errors="replace") if body else "unknown")


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore,
    headers: dict,
) -> Optional[Union[dict, list]]:
    """Fetch a GitHub API JSON endpoint. Returns parsed JSON or None."""
    async with semaphore:
        for attempt in range(MAX_RETRIES):
            await _throttle_github()
            try:
                async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status == 403:
                        body = await resp.read()
                        msg = _parse_403_body(body)
                        retry_after = resp.headers.get("Retry-After")
                        wait = int(retry_after) if retry_after and retry_after.isdigit() else (60 * (attempt + 1))
                        logger.warning(
                            "HTTP 403 ... GitHub says: %s. Waiting %ds before retry %d/%d",
                            msg, wait, attempt + 1, MAX_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.warning("HTTP %d for %s", resp.status, url)
                    return None
            except Exception as e:
                err_msg = str(e) if str(e) else type(e).__name__
                logger.warning("Request failed for %s: %s", url[:80], err_msg)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                return None
        return None


async def _fetch_raw(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore,
    headers: dict,
) -> Optional[str]:
    """Fetch a URL as raw text. Returns content string or None."""
    async with semaphore:
        for attempt in range(MAX_RETRIES):
            await _throttle_github()
            try:
                async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        return raw.decode("utf-8", errors="replace")
                    if resp.status == 403:
                        body = await resp.read()
                        msg = _parse_403_body(body)
                        retry_after = resp.headers.get("Retry-After")
                        wait = int(retry_after) if retry_after and retry_after.isdigit() else (60 * (attempt + 1))
                        logger.warning(
                            "HTTP 403 ... GitHub says: %s. Waiting %ds before retry %d/%d",
                            msg, wait, attempt + 1, MAX_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.warning("HTTP %d for %s", resp.status, url)
                    return None
            except Exception as e:
                err_msg = str(e) if str(e) else type(e).__name__
                logger.warning("Request failed for %s: %s", url[:80], err_msg)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                return None
        return None


async def fetch_pr_metadata(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    owner: str,
    repo: str,
    pr_number: str,
    headers: dict,
) -> Optional[dict]:
    """
    Fetch PR metadata: head_sha, head_owner, head_repo, list of changed files.
    For fork PRs, we must use head repo when fetching file contents (head_sha
    exists only there). Returns {"head_sha", "head_owner", "head_repo", "files"}.
    """
    pr_url    = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    files_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100"

    pr_data, files_data = await asyncio.gather(
        _fetch_json(session, pr_url,    semaphore, headers),
        _fetch_json(session, files_url, semaphore, headers),
    )

    if pr_data is None or not isinstance(pr_data, dict):
        return None

    head = pr_data.get("head", {})
    head_sha = head.get("sha", "")
    head_repo = head.get("repo")
    head_owner = owner
    head_repo_name = repo
    if isinstance(head_repo, dict):
        head_owner = head_repo.get("owner", {}).get("login", owner)
        head_repo_name = head_repo.get("name", repo)

    changed_files = []
    if isinstance(files_data, list):
        for f in files_data:
            filename = f.get("filename", "")
            status   = f.get("status", "modified")  # added, modified, removed, renamed
            if filename and status != "removed":
                changed_files.append({"path": filename, "status": status})

    return {
        "head_sha": head_sha,
        "head_owner": head_owner,
        "head_repo": head_repo_name,
        "files": changed_files,
    }


async def fetch_file_content(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    owner: str,
    repo: str,
    path: str,
    sha: str,
    headers: dict,
) -> Optional[str]:
    """
    Fetch full file content via GitHub Contents API.
    Returns decoded UTF-8 string or None.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}?ref={sha}"
    data = await _fetch_json(session, url, semaphore, headers)

    if not isinstance(data, dict):
        return None

    encoding = data.get("encoding", "")
    content  = data.get("content", "")
    size     = data.get("size", 0)

    if size > MAX_FILE_BYTES:
        logger.debug("Skipping %s: file too large (%d bytes)", path, size)
        return None

    if encoding == "base64" and content:
        try:
            return base64.b64decode(content.replace("\n", "")).decode("utf-8", errors="replace")
        except Exception as e:
            logger.debug("Base64 decode failed for %s: %s", path, e)
            return None

    return None


def _is_static_analysis_target(path: str) -> bool:
    """Return True if the file extension is one we run static analysis on."""
    return any(path.lower().endswith(ext) for ext in STATIC_ANALYSIS_EXTS)


def _parse_semester(raw: str) -> tuple[str, str, Optional[int]]:
    """
    Parse semester string to (semester, season, year).
    "Spring 2025 Final" -> ("Spring 2025", "Spring", 2025)
    "2019 Fall Final" -> ("Fall 2019", "Fall", 2019)  # older format: year first
    """
    if pd.isna(raw) or not str(raw).strip():
        return ("", "", None)
    s = str(raw).strip()
    # Format 1: "Season YYYY" (e.g. Spring 2025 Final, Fall 2023 final)
    m = re.search(r"^(\w+)\s+(\d{4})", s)
    if m:
        first, year_str = m.group(1), m.group(2)
        year = int(year_str)
        if not first.isdigit():
            return (f"{first} {year}", first, year)
    # Format 2: "YYYY Season" (e.g. 2019 Fall Final, 2018 Spring OSS)
    m2 = re.search(r"^(\d{4})\s+(\w+)", s)
    if m2:
        year_str, season = m2.group(1), m2.group(2)
        year = int(year_str)
        return (f"{season} {year}", season, year)
    return (s, "", None)


def _cache_filename_to_path(fname: str) -> str:
    """Convert 984e4438974d_app__controllers__x.rb -> app/controllers/x.rb"""
    if "_" not in fname:
        return fname
    parts = fname.split("_", 1)
    if len(parts) == 2 and len(parts[0]) >= 12:
        return parts[1].replace("__", "/")
    return fname.replace("__", "/")


def _project_id_to_year(project_id: str) -> Optional[int]:
    """
    Extract year from project ID. E2541 -> 2025, E1998 -> 2019.
    Format: E + YY + XX where YY is 2-digit year.
    """
    s = str(project_id).strip()
    if not s.startswith("E") or len(s) < 4:
        return None
    try:
        yy = int(s[1:3])  # chars 1-2: "25" or "19"
        if 10 <= yy <= 99:  # allow 2010-2099 (was yy>=20, excluded 2016-2019)
            return 2000 + yy
    except ValueError:
        pass
    return None


def _project_matches_years(project_id: str, years: list[int]) -> bool:
    """Return True if project_id's year is in the given list."""
    if not years:
        return True
    y = _project_id_to_year(project_id)
    return y is not None and y in years


# ─────────────────────────────────────────────────────────────────────────────
# Per-record processing
# ─────────────────────────────────────────────────────────────────────────────
async def process_record(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    record: dict,
    github_headers: dict,
    diff_headers: dict,
    wiki_headers: dict,
) -> dict:
    """
    Process one project record: fetch diff, wiki, and full file content.
    Returns the enriched record dict.
    """
    links    = record.get("links_raw", "")
    pr_specs = parse_github_pr_urls(links)
    pr_urls, repo_name = parse_pr_urls_and_repo(links)
    wiki_urls = extract_wiki_urls(links)

    # Fetch all PR diffs (same as extract_data.py)
    pr_api_urls = [
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_num}"
        for owner, repo, pr_num in pr_specs
    ]
    diff_tasks = [
        _fetch_raw(session, url, semaphore, diff_headers)
        for url in pr_api_urls
    ]
    wiki_tasks = [
        _fetch_raw(session, url, semaphore, wiki_headers)
        for url in wiki_urls
    ]

    diff_contents = await asyncio.gather(*diff_tasks)
    wiki_contents = await asyncio.gather(*wiki_tasks)

    diff_text = "\n\n---\n\n".join(c for c in diff_contents if c)
    wiki_text = "\n\n---\n\n".join(
        extract_main_content(c) for c in wiki_contents if c
    )

    # ── Fetch full file content for static analysis ──────────────────────────
    full_files: list[dict] = []

    for (owner, repo, pr_num) in pr_specs:
        metadata = await fetch_pr_metadata(
            session, semaphore, owner, repo, pr_num, github_headers
        )
        if not metadata:
            continue

        head_sha = metadata.get("head_sha", "")
        if not head_sha:
            logger.debug("PR %s/%s#%s – no head_sha, skipping full-file fetch", owner, repo, pr_num)
            continue

        head_owner = metadata.get("head_owner", owner)
        head_repo = metadata.get("head_repo", repo)
        targets  = [
            f for f in metadata["files"]
            if _is_static_analysis_target(f["path"])
        ]

        logger.debug(
            "PR %s/%s#%s – head=%s/%s@%s, %d analysable file(s)",
            owner, repo, pr_num, head_owner, head_repo, head_sha[:7] if head_sha else "?", len(targets),
        )

        content_tasks = [
            fetch_file_content(
                session, semaphore, head_owner, head_repo, f["path"], head_sha, github_headers
            )
            for f in targets
        ]
        contents = await asyncio.gather(*content_tasks)

        for file_info, content in zip(targets, contents):
            if content is not None:
                full_files.append({
                    "path":    file_info["path"],
                    "content": content,
                })

    return {
        "project_id":       record["project_id"],
        "semester":         record.get("semester", ""),
        "season":           record.get("season", ""),
        "year":             record.get("year"),
        "pr_urls":          pr_urls,
        "repo_name":        repo_name,
        "diff":             diff_text,
        "wiki_content":     wiki_text,
        "project_feedback": record.get("project_feedback", ""),
        "design_feedback":  record.get("design_feedback", ""),
        "full_files":       full_files,
    }


# ─────────────────────────────────────────────────────────────────────────────
# From-cache pipeline (no GitHub API)
# ─────────────────────────────────────────────────────────────────────────────
def run_from_cache(
    csv_path: Path,
    output_path: Path,
    cache_dir: Path,
    limit: Optional[int] = None,
    years: Optional[list[int]] = None,
) -> None:
    """
    Build dataset JSONL entirely from pr_cache/ without hitting GitHub API.
    Reads projects.csv for project_id, feedback, semester; reads pr_cache/<project_id>/ for diff, wiki, files.
    """
    logger.info("Reading %s", csv_path)
    df = pd.read_csv(csv_path, quoting=1)
    df.columns = df.columns.str.strip()
    df.iloc[:, 0] = df.iloc[:, 0].replace("", pd.NA).ffill()

    required_cols = ["Project ID", "Feedback on project", "Feedback on design doc"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.error("Missing columns: %s. Available: %s", missing, list(df.columns))
        sys.exit(1)

    first_col = df.iloc[:, 0]
    records = []
    for idx, row in df.iterrows():
        pid = row.get("Project ID", "")
        if pd.isna(pid) or not str(pid).strip():
            continue
        links_raw = row.get("links", "")
        if pd.isna(links_raw):
            links_raw = ""
        else:
            links_raw = str(links_raw).strip()

        semester_raw = first_col.iloc[idx] if idx < len(first_col) else ""
        semester, season, year = _parse_semester(semester_raw)

        records.append({
            "project_id":       str(pid).strip(),
            "semester":         semester,
            "season":           season,
            "year":             year,
            "links_raw":        links_raw,
            "project_feedback": "" if pd.isna(row.get("Feedback on project", "")) else str(row.get("Feedback on project", "")).strip(),
            "design_feedback":  "" if pd.isna(row.get("Feedback on design doc", "")) else str(row.get("Feedback on design doc", "")).strip(),
        })

    if years:
        records = [
            r for r in records
            if (r.get("year") in years) or (_project_id_to_year(r["project_id"]) in years)
        ]
        logger.info("Filtered to year(s) %s: %d project(s)", years, len(records))

    if limit:
        records = records[:limit]

    logger.info("Building %d record(s) from cache", len(records))

    results = []
    for rec in tqdm(records, desc="From cache"):
        proj_dir = cache_dir / rec["project_id"]
        if not proj_dir.exists():
            logger.debug("Cache missing for %s, skipping", rec["project_id"])
            pr_urls, repo_name = parse_pr_urls_and_repo(rec.get("links_raw", ""))
            results.append({
                "project_id":       rec["project_id"],
                "semester":         rec["semester"],
                "season":           rec["season"],
                "year":             rec["year"],
                "pr_urls":          pr_urls,
                "repo_name":        repo_name,
                "diff":             "",
                "wiki_content":     "",
                "project_feedback": rec["project_feedback"],
                "design_feedback":  rec["design_feedback"],
                "full_files":       [],
            })
            continue

        diff = ""
        diff_path = proj_dir / "diff.txt"
        if diff_path.exists():
            diff = diff_path.read_text(encoding="utf-8", errors="replace")

        wiki_content = ""
        wiki_path = proj_dir / "wiki.txt"
        if wiki_path.exists():
            wiki_content = wiki_path.read_text(encoding="utf-8", errors="replace")

        project_feedback = rec["project_feedback"]
        design_feedback = rec["design_feedback"]
        meta_path = proj_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                project_feedback = meta.get("project_feedback", project_feedback)
                design_feedback = meta.get("design_feedback", design_feedback)
            except Exception:
                pass

        full_files: list[dict] = []
        files_dir = proj_dir / "files"
        if files_dir.exists():
            for p in files_dir.iterdir():
                if p.suffix.lower() in STATIC_ANALYSIS_EXTS:
                    try:
                        content = p.read_text(encoding="utf-8", errors="replace")
                        path = _cache_filename_to_path(p.name)
                        full_files.append({"path": path, "content": content})
                    except Exception:
                        pass

        pr_urls, repo_name = parse_pr_urls_and_repo(rec.get("links_raw", ""))
        results.append({
            "project_id":       rec["project_id"],
            "semester":         rec["semester"],
            "season":           rec["season"],
            "year":             rec["year"],
            "pr_urls":          pr_urls,
            "repo_name":        repo_name,
            "diff":             diff,
            "wiki_content":     wiki_content,
            "project_feedback": project_feedback,
            "design_feedback":  design_feedback,
            "full_files":       full_files,
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    files_count = sum(len(r.get("full_files", [])) for r in results)
    logger.info("Wrote %d records (%d full file(s)) to %s", len(results), files_count, output_path)


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────
async def main_async(
    csv_path: Path,
    output_path: Path,
    limit: Optional[int] = None,
    years: Optional[list[int]] = None,
    concurrency: int = MAX_CONCURRENT,
    delay: float = MIN_REQUEST_GAP,
) -> None:
    logger.info("Reading %s", csv_path)
    df = pd.read_csv(csv_path, quoting=1)
    df.columns = df.columns.str.strip()
    # Forward-fill first column for merged cells (Google Sheets export)
    df.iloc[:, 0] = df.iloc[:, 0].replace("", pd.NA).ffill()

    required_cols = ["Project ID", "links", "Feedback on project", "Feedback on design doc"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.error("Missing columns: %s. Available: %s", missing, list(df.columns))
        sys.exit(1)

    records = []
    first_col = df.iloc[:, 0]
    for idx, row in df.iterrows():
        pid = row.get("Project ID", "")
        if pd.isna(pid) or not str(pid).strip():
            continue
        links_raw = row.get("links", "")
        if pd.isna(links_raw):
            links_raw = ""
        else:
            links_raw = str(links_raw).strip()

        semester_raw = first_col.iloc[idx] if idx < len(first_col) else ""
        semester, season, year = _parse_semester(semester_raw)

        records.append({
            "project_id":       str(pid).strip(),
            "semester":         semester,
            "season":           season,
            "year":             year,
            "links_raw":        links_raw,
            "project_feedback": "" if pd.isna(row.get("Feedback on project", "")) else str(row.get("Feedback on project", "")).strip(),
            "design_feedback":  "" if pd.isna(row.get("Feedback on design doc", "")) else str(row.get("Feedback on design doc", "")).strip(),
        })

    if years:
        records = [r for r in records if _project_matches_years(r["project_id"], years)]
        logger.info("Filtered to year(s) %s: %d project(s)", years, len(records))

    if limit:
        records = records[:limit]

    logger.info("Processing %d project(s)", len(records))

    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
        logger.warning(
            "No GITHUB_TOKEN set. GitHub allows 60 req/hr unauthenticated. "
            "Diffs and full_files will likely fail (403). Set GITHUB_TOKEN for 5000/hr."
        )

    global _rate_limit_lock
    _rate_limit_lock = asyncio.Lock()  # Must be created inside event loop
    _throttle_config["min_gap"] = delay
    logger.info("Throttle: %d concurrent, %.1fs between requests", concurrency, delay)

    github_headers = _make_github_headers()
    diff_headers   = {**github_headers, "Accept": GITHUB_DIFF_ACCEPT}
    wiki_headers   = {"User-Agent": USER_AGENT}

    semaphore = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession() as session:
        # Validate token and log rate limit before starting
        rate_url = f"{GITHUB_API_BASE}/rate_limit"
        await _throttle_github()
        try:
            async with session.get(rate_url, headers=github_headers, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    core = data.get("resources", {}).get("core", {})
                    logger.info(
                        "GitHub API: %d/%d requests remaining (resets %s)",
                        core.get("remaining", "?"),
                        core.get("limit", "?"),
                        core.get("reset", "?"),
                    )
                else:
                    body = await r.read()
                    logger.warning(
                        "Token check failed (HTTP %d): %s",
                        r.status, _parse_403_body(body),
                    )
                    if r.status == 403:
                        logger.warning(
                            "If using a fine-grained token: grant 'Contents: Read' and 'Pull requests: Read' "
                            "for the repos. If classic: ensure 'repo' scope."
                        )
        except Exception as e:
            logger.warning("Could not check rate limit: %s", e)

        tasks = [
            process_record(session, semaphore, rec, github_headers, diff_headers, wiki_headers)
            for rec in records
        ]
        results = await tqdm.gather(*tasks, desc="Extracting PRs + full files")

    with open(output_path, "w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    files_fetched = sum(len(r.get("full_files", [])) for r in results)
    logger.info(
        "Wrote %d records (%d full file(s) fetched) to %s",
        len(results), files_fetched, output_path,
    )


def _run_validate_token() -> None:
    """Validate GITHUB_TOKEN and show rate limit."""
    headers = _make_github_headers()
    if "Authorization" not in headers:
        logger.error("No GITHUB_TOKEN or GH_TOKEN set. Cannot validate.")
        sys.exit(1)

    async def _check() -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{GITHUB_API_BASE}/rate_limit",
                headers=headers,
                timeout=10,
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    core = data.get("resources", {}).get("core", {})
                    print(f"Token OK. Rate limit: {core.get('remaining')}/{core.get('limit')} remaining")
                    print(f"Resets at: {core.get('reset')}")
                else:
                    body = await r.read()
                    print(f"Token invalid (HTTP {r.status}): {_parse_403_body(body)}")
                    if r.status == 403:
                        print("Fine-grained token: grant 'Contents: Read' + 'Pull requests: Read' for target repos.")
                        print("Classic token: ensure 'repo' scope.")

    asyncio.run(_check())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract PR diffs, Wiki content, AND full file content "
            "for static analysis (hybrid pipeline v2)."
        )
    )
    parser.add_argument("--input",  "-i", type=Path, default=Path("projects.csv"))
    parser.add_argument("--output", "-o", type=Path, default=Path("dataset_v2.jsonl"))
    parser.add_argument("--cache",  "-c", type=Path, default=Path("pr_cache"), help="pr_cache directory (for --from-cache)")
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Build dataset from pr_cache/ only (no GitHub API). Instant, no rate limits.",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Only process first N projects (useful for testing).",
    )
    parser.add_argument(
        "--year",
        type=str,
        default=None,
        metavar="YEAR",
        help="Filter by year(s) from project ID (E25xx=2025, E24xx=2024). E.g. 2025 or 2024,2025.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=MAX_CONCURRENT,
        help=f"Max concurrent GitHub requests (default {MAX_CONCURRENT}). Lower if 403 persists.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=MIN_REQUEST_GAP,
        help=f"Min seconds between requests (default {MIN_REQUEST_GAP}). Increase if 403 persists.",
    )
    parser.add_argument(
        "--validate-token",
        action="store_true",
        help="Only validate GITHUB_TOKEN and show rate limit, then exit.",
    )
    args = parser.parse_args()

    if args.validate_token:
        _run_validate_token()
        return

    if not args.input.exists():
        logger.error("Input not found: %s", args.input)
        sys.exit(1)

    years = None
    if args.year:
        try:
            years = [int(y.strip()) for y in args.year.split(",") if y.strip()]
            if not years:
                logger.error("--year must specify at least one year (e.g. 2025 or 2024,2025)")
                sys.exit(1)
        except ValueError:
            logger.error("--year must be integers (e.g. 2025 or 2024,2025)")
            sys.exit(1)

    if args.from_cache:
        run_from_cache(args.input, args.output, args.cache, args.limit, years)
        return

    asyncio.run(main_async(
        args.input, args.output, args.limit,
        years=years,
        concurrency=args.concurrency,
        delay=args.delay,
    ))


if __name__ == "__main__":
    main()
