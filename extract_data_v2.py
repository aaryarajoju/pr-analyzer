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

REQUEST_TIMEOUT   = 30
MAX_CONCURRENT    = 10
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
# GitHub API helpers
# ─────────────────────────────────────────────────────────────────────────────
def _make_github_headers(token: Optional[str] = None) -> dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Accept": GITHUB_JSON_ACCEPT}
    tok = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore,
    headers: dict,
) -> Optional[Union[dict, list]]:
    """Fetch a GitHub API JSON endpoint. Returns parsed JSON or None."""
    async with semaphore:
        try:
            async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                logger.warning("HTTP %d for %s", resp.status, url)
                return None
        except Exception as e:
            logger.warning("Request failed for %s: %s", url, e)
            return None


async def _fetch_raw(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore,
    headers: dict,
) -> Optional[str]:
    """Fetch a URL as raw text. Returns content string or None."""
    async with semaphore:
        try:
            async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    raw = await resp.read()
                    return raw.decode("utf-8", errors="replace")
                logger.warning("HTTP %d for %s", resp.status, url)
                return None
        except Exception as e:
            logger.warning("Request failed for %s: %s", url, e)
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
    Fetch PR metadata: head_sha + list of changed files.
    Returns {"head_sha": str, "files": [{"path": str, "status": str}]}
    """
    pr_url    = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    files_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100"

    pr_data, files_data = await asyncio.gather(
        _fetch_json(session, pr_url,    semaphore, headers),
        _fetch_json(session, files_url, semaphore, headers),
    )

    if pr_data is None or not isinstance(pr_data, dict):
        return None

    head_sha = pr_data.get("head", {}).get("sha", "")
    changed_files = []

    if isinstance(files_data, list):
        for f in files_data:
            filename = f.get("filename", "")
            status   = f.get("status", "modified")  # added, modified, removed, renamed
            if filename and status != "removed":
                changed_files.append({"path": filename, "status": status})

    return {"head_sha": head_sha, "files": changed_files}


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

        head_sha = metadata["head_sha"]
        targets  = [
            f for f in metadata["files"]
            if _is_static_analysis_target(f["path"])
        ]

        logger.debug(
            "PR %s/%s#%s – head_sha=%s, %d analysable file(s)",
            owner, repo, pr_num, head_sha[:7], len(targets),
        )

        content_tasks = [
            fetch_file_content(
                session, semaphore, owner, repo, f["path"], head_sha, github_headers
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
        "diff":             diff_text,
        "wiki_content":     wiki_text,
        "project_feedback": record.get("project_feedback", ""),
        "design_feedback":  record.get("design_feedback", ""),
        "full_files":       full_files,  # NEW: [{path, content}]
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────
async def main_async(
    csv_path: Path,
    output_path: Path,
    limit: Optional[int] = None,
) -> None:
    logger.info("Reading %s", csv_path)
    df = pd.read_csv(csv_path, quoting=1)
    df.columns = df.columns.str.strip()

    required_cols = ["Project ID", "links", "Feedback on project", "Feedback on design doc"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.error("Missing columns: %s. Available: %s", missing, list(df.columns))
        sys.exit(1)

    records = []
    for _, row in df.iterrows():
        pid = row.get("Project ID", "")
        if pd.isna(pid) or not str(pid).strip():
            continue
        records.append({
            "project_id":       str(pid).strip(),
            "links_raw":        row.get("links", ""),
            "project_feedback": "" if pd.isna(row.get("Feedback on project", "")) else str(row.get("Feedback on project", "")).strip(),
            "design_feedback":  "" if pd.isna(row.get("Feedback on design doc", "")) else str(row.get("Feedback on design doc", "")).strip(),
        })

    if limit:
        records = records[:limit]

    logger.info("Processing %d project(s)", len(records))

    github_headers = _make_github_headers()
    diff_headers   = {**github_headers, "Accept": GITHUB_DIFF_ACCEPT}
    wiki_headers   = {"User-Agent": USER_AGENT}

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with aiohttp.ClientSession() as session:
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract PR diffs, Wiki content, AND full file content "
            "for static analysis (hybrid pipeline v2)."
        )
    )
    parser.add_argument("--input",  "-i", type=Path, default=Path("projects.csv"))
    parser.add_argument("--output", "-o", type=Path, default=Path("dataset_v2.jsonl"))
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Only process first N projects (useful for testing).",
    )
    args = parser.parse_args()

    if not args.input.exists():
        logger.error("Input not found: %s", args.input)
        sys.exit(1)

    asyncio.run(main_async(args.input, args.output, args.limit))


if __name__ == "__main__":
    main()
