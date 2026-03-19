#!/usr/bin/env python3
"""
predownload_prs.py – Pre-download all PR data to disk.

Pre-downloads diffs, wiki content, and full file content for .rb/.ts/.tsx files
so evaluation never hits the GitHub API. Fully standalone; no project imports.

Usage:
    python predownload_prs.py --input projects.csv --output pr_cache/ --delay 0.5 --limit 10

Requires: GITHUB_TOKEN or GH_TOKEN for GitHub API (5000 req/hr vs 60 unauthenticated).
"""

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Union

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
GITHUB_PR_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:/.*)?",
    re.IGNORECASE,
)
WIKI_PATTERN = re.compile(
    r"https?://(?:www\.)?wiki\.expertiza\.ncsu\.edu/[^\s)\]\"]+",
    re.IGNORECASE,
)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_DIFF_ACCEPT = "application/vnd.github.v3.diff"
GITHUB_JSON_ACCEPT = "application/vnd.github.v3+json"
USER_AGENT = "Mozilla/5.0 (compatible; ThesisPRAnalyzer/predownload)"
REQUEST_TIMEOUT = 60
MAX_FILE_BYTES = 500_000
STATIC_ANALYSIS_EXTS = {".rb", ".ts", ".tsx"}
MAX_RETRIES = 3
RETRY_WAIT = 60


def _make_headers(accept: str = GITHUB_JSON_ACCEPT) -> dict[str, str]:
    h = {"User-Agent": USER_AGENT, "Accept": accept}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def parse_github_pr_urls(links_text: str) -> list[tuple[str, str, str]]:
    """Extract (owner, repo, pr_number) tuples from links string."""
    if not links_text or not str(links_text).strip():
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
    if not links_text or not str(links_text).strip():
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
    for sel in ["#mw-content-text", ".mw-parser-output", "main", "article", "#content", "body"]:
        if sel.startswith("#"):
            content = soup.find(id=sel[1:])
        elif sel.startswith("."):
            content = soup.find(class_=sel[1:])
        else:
            content = soup.find(sel)
        if content:
            text = content.get_text(separator="\n", strip=True)
            break
    else:
        text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def safe_filename(path: str) -> str:
    """Replace / with __ for filesystem-safe name."""
    return path.replace("/", "__")


def fetch_with_retry(
    url: str,
    headers: dict,
    is_json: bool = True,
    delay: float = 0,
) -> tuple[Optional[Union[dict, list, str]], Optional[str]]:
    """
    Fetch URL with retries on 403. Returns (data, error_msg).
    On success: (data, None). On final failure: (None, error_msg).
    """
    for attempt in range(MAX_RETRIES):
        if delay > 0:
            time.sleep(delay)
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                if is_json:
                    return r.json(), None
                return r.text, None
            if r.status_code == 403:
                retry_after = r.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and str(retry_after).isdigit() else RETRY_WAIT
                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                    continue
                try:
                    msg = r.json().get("message", str(r.text)[:200])
                except Exception:
                    msg = str(r.text)[:200]
                return None, f"HTTP 403: {msg}"
            return None, f"HTTP {r.status_code}"
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_WAIT)
                continue
            return None, str(e)
    return None, "Max retries exceeded"


def process_project(
    project_id: str,
    links_raw: str,
    project_feedback: str,
    design_feedback: str,
    output_dir: Path,
    github_headers: dict,
    diff_headers: dict,
    wiki_headers: dict,
    delay: float,
    error_log: list[tuple[str, str]],
) -> tuple[int, int, int]:
    """
    Process one project. Returns (files_downloaded, files_skipped, errors).
    """
    project_dir = output_dir / project_id
    files_dir = project_dir / "files"
    meta_path = project_dir / "meta.json"

    pr_specs = parse_github_pr_urls(links_raw)
    wiki_urls = extract_wiki_urls(links_raw)

    # Resumable: skip if meta.json exists and files/ has content
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stored = meta.get("stored_files", [])
            if stored and files_dir.exists() and any(files_dir.iterdir()):
                return 0, len(stored), 0
        except (json.JSONDecodeError, OSError):
            pass

    project_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(exist_ok=True)

    diff_parts = []
    wiki_parts = []
    stored_files = []
    files_downloaded = 0
    files_skipped = 0
    errors = 0

    # Fetch PR diffs and metadata
    pr_urls_saved = []
    for owner, repo, pr_num in pr_specs:
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_num}"
        pr_urls_saved.append(pr_url)

        diff_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_num}"
        data, err = fetch_with_retry(diff_url, diff_headers, is_json=False, delay=delay)
        if err:
            error_log.append((project_id, f"PR diff {pr_url}: {err}"))
            errors += 1
            continue
        if data:
            diff_parts.append(data)

        # Metadata for file fetch
        meta_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_num}"
        meta_data, err = fetch_with_retry(meta_url, github_headers, is_json=True, delay=delay)
        if err or not isinstance(meta_data, dict):
            continue

        head = meta_data.get("head", {})
        full_sha = head.get("sha") or ""
        head_sha = full_sha[:12]
        head_repo = head.get("repo") or {}
        head_owner = head_repo.get("owner", {}).get("login", owner)
        head_repo_name = head_repo.get("name", repo)

        files_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_num}/files?per_page=100"
        files_data, err = fetch_with_retry(files_url, github_headers, is_json=True, delay=delay)
        if err or not isinstance(files_data, list):
            continue

        targets = [
            f for f in files_data
            if isinstance(f, dict)
            and f.get("status") != "removed"
            and any((f.get("filename") or "").lower().endswith(ext) for ext in STATIC_ANALYSIS_EXTS)
        ]

        for f in targets:
            path = f.get("filename", "")
            if not path:
                continue
            ext = Path(path).suffix.lower()
            if ext not in STATIC_ANALYSIS_EXTS:
                continue
            safe = safe_filename(path)
            fname = f"{head_sha}_{safe}"
            fpath = files_dir / fname
            if fpath.exists():
                files_skipped += 1
                stored_files.append(fname)
                continue

            content_url = f"{GITHUB_API_BASE}/repos/{head_owner}/{head_repo_name}/contents/{path}?ref={full_sha}"
            data, err = fetch_with_retry(content_url, github_headers, is_json=True, delay=delay)
            if err:
                error_log.append((project_id, f"File {path}: {err}"))
                errors += 1
                continue
            if not isinstance(data, dict):
                continue
            size = data.get("size", 0)
            if size > MAX_FILE_BYTES:
                files_skipped += 1
                continue
            enc = data.get("encoding", "")
            content_b64 = data.get("content", "")
            if enc == "base64" and content_b64:
                try:
                    content = base64.b64decode(content_b64.replace("\n", "")).decode("utf-8", errors="replace")
                except Exception:
                    continue
            else:
                continue
            fpath.write_text(content, encoding="utf-8")
            files_downloaded += 1
            stored_files.append(fname)

    # Fetch Wiki
    for url in wiki_urls:
        data, err = fetch_with_retry(url, wiki_headers, is_json=False, delay=delay)
        if err:
            error_log.append((project_id, f"Wiki {url}: {err}"))
            errors += 1
            continue
        if data:
            wiki_parts.append(extract_main_content(data))

    # Write outputs
    diff_text = "\n\n---\n\n".join(diff_parts) if diff_parts else ""
    wiki_text = "\n\n---\n\n".join(wiki_parts) if wiki_parts else ""

    (project_dir / "diff.txt").write_text(diff_text, encoding="utf-8")
    (project_dir / "wiki.txt").write_text(wiki_text, encoding="utf-8")

    meta = {
        "project_id": project_id,
        "pr_urls": pr_urls_saved,
        "wiki_urls": wiki_urls,
        "project_feedback": project_feedback or "",
        "design_feedback": design_feedback or "",
        "stored_files": stored_files,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return files_downloaded, files_skipped, errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-download PR data to disk for offline evaluation.",
    )
    parser.add_argument("--input", "-i", type=Path, default=Path("projects.csv"))
    parser.add_argument("--output", "-o", type=Path, default=Path("pr_cache"))
    parser.add_argument("--delay", "-d", type=float, default=0.5)
    parser.add_argument("--limit", "-n", type=int, default=None)
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: Input not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
        print("Warning: No GITHUB_TOKEN or GH_TOKEN. GitHub allows 60 req/hr unauthenticated.", file=sys.stderr)

    args.output.mkdir(parents=True, exist_ok=True)
    error_log: list[tuple[str, str]] = []
    error_log_path = args.output.parent / "predownload_errors.log"

    github_headers = _make_headers(GITHUB_JSON_ACCEPT)
    diff_headers = _make_headers(GITHUB_DIFF_ACCEPT)
    wiki_headers = {"User-Agent": USER_AGENT}

    records = []
    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            cols = [c.strip() for c in reader.fieldnames]
        else:
            cols = []
        for row in reader:
            pid = (row.get("Project ID") or row.get(cols[0] if cols else "")) or ""
            if not str(pid).strip():
                continue
            links = row.get("links", row.get("Links", ""))
            pf = row.get("Feedback on project", "")
            df = row.get("Feedback on design doc", "")
            records.append({
                "project_id": str(pid).strip(),
                "links_raw": "" if not links else str(links).strip(),
                "project_feedback": "" if not pf else str(pf).strip(),
                "design_feedback": "" if not df else str(df).strip(),
            })

    if args.limit:
        records = records[: args.limit]

    total_downloaded = 0
    total_skipped = 0
    total_errors = 0
    total_projects = len(records)

    for rec in tqdm(records, desc="Projects"):
        d, s, e = process_project(
            rec["project_id"],
            rec["links_raw"],
            rec["project_feedback"],
            rec["design_feedback"],
            args.output,
            github_headers,
            diff_headers,
            wiki_headers,
            args.delay,
            error_log,
        )
        total_downloaded += d
        total_skipped += s
        total_errors += e

    if error_log:
        with open(error_log_path, "w", encoding="utf-8") as f:
            for pid, msg in error_log:
                f.write(f"{pid}\t{msg}\n")
        print(f"\nErrors logged to {error_log_path}", file=sys.stderr)

    print("\n" + "=" * 50)
    print("Summary")
    print("=" * 50)
    print(f"Total projects:     {total_projects}")
    print(f"Files downloaded:   {total_downloaded}")
    print(f"Files skipped:      {total_skipped}")
    print(f"Errors:             {total_errors}")


if __name__ == "__main__":
    main()
