#!/usr/bin/env python3
"""
Extract design principle data from projects.csv for thesis analysis.

Reads projects.csv, extracts GitHub PR diffs and Wiki content from links,
and outputs dataset_raw.jsonl with project_id, diff, wiki_content, feedback fields.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

import aiohttp
import pandas as pd
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

# Regex patterns for URL extraction
GITHUB_PR_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:/.*)?",
    re.IGNORECASE,
)
WIKI_PATTERN = re.compile(
    r"https?://(?:www\.)?wiki\.expertiza\.ncsu\.edu/[^\s)\]\"]+",
    re.IGNORECASE,
)

# HTTP client settings
REQUEST_TIMEOUT = 30
MAX_CONCURRENT = 10  # Limit concurrent requests to avoid rate limiting
USER_AGENT = "Mozilla/5.0 (compatible; ThesisPRAnalyzer/1.0; +https://github.com/ncsu)"
# GitHub API: Accept header for raw diff (avoids redirect to patch-diff.githubusercontent.com)
GITHUB_DIFF_ACCEPT = "application/vnd.github.v3.diff"


def extract_github_pr_api_urls(links_text: str) -> list[str]:
    """
    Extract unique GitHub PR URLs and convert to API diff URLs.
    Uses api.github.com instead of .diff (which redirects to patch-diff.githubusercontent.com
    and can cause SSL WRONG_VERSION_NUMBER errors behind proxies).
    """
    if pd.isna(links_text) or not str(links_text).strip():
        return []
    urls = set()
    for match in GITHUB_PR_PATTERN.finditer(str(links_text)):
        owner, repo, pr_num = match.groups()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}"
        urls.add(api_url)
    return list(urls)


def extract_wiki_urls(links_text: str) -> list[str]:
    """Extract unique Wiki URLs from links text."""
    if pd.isna(links_text) or not str(links_text).strip():
        return []
    urls = []
    seen = set()
    for match in WIKI_PATTERN.finditer(str(links_text)):
        url = match.group(0).rstrip(".,;:)")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_main_content(html: str) -> str:
    """Extract main content text from Wiki HTML using BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove script and style elements
    for tag in soup(["script", "style"]):
        tag.decompose()

    # Try common content containers first (MediaWiki structure)
    content_selectors = [
        "#mw-content-text",
        ".mw-parser-output",
        "main",
        "article",
        "#content",
        "body",
    ]
    content = None
    for selector in content_selectors:
        if selector.startswith("#"):
            content = soup.find(id=selector[1:])
        elif selector.startswith("."):
            content = soup.find(class_=selector[1:])
        else:
            content = soup.find(selector)
        if content:
            break

    if content:
        text = content.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    # Normalize whitespace
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


async def fetch_url(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore,
    headers: Optional[dict] = None,
) -> tuple[str, Optional[str]]:
    """
    Fetch URL content. Returns (url, content) or (url, None) on failure.
    """
    async with semaphore:
        try:
            req_headers = {"User-Agent": USER_AGENT}
            if headers:
                req_headers.update(headers)
            async with session.get(url, headers=req_headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    # Use read() + decode with errors='replace' to handle binary content in diffs
                    raw = await resp.read()
                    return (url, raw.decode("utf-8", errors="replace"))
                logger.warning("HTTP %d for %s", resp.status, url)
                return (url, None)
        except asyncio.TimeoutError:
            logger.warning("Timeout fetching %s", url)
            return (url, None)
        except aiohttp.ClientError as e:
            logger.warning("Failed to fetch %s: %s", url, e)
            return (url, None)
        except Exception as e:
            logger.warning("Unexpected error fetching %s: %s", url, e)
            return (url, None)


async def fetch_urls(urls: list[str], extra_headers: Optional[dict] = None) -> dict[str, str]:
    """Fetch multiple URLs concurrently; return {url: content} for successful fetches."""
    if not urls:
        return {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    results = {}
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_url(session, url, semaphore, extra_headers) for url in urls]
        done = await tqdm.gather(*tasks, desc="Fetching URLs")
        for url, content in done:
            if content is not None:
                results[url] = content
    return results


def process_row(row: pd.Series) -> Optional[dict]:
    """Process a single row into the output record format."""
    project_id = row.get("Project ID", "")
    if pd.isna(project_id) or not str(project_id).strip():
        return None

    project_id = str(project_id).strip()
    links = row.get("links", "")
    project_feedback = row.get("Feedback on project", "")
    design_feedback = row.get("Feedback on design doc", "")

    # Normalize feedback (handle NaN)
    project_feedback = "" if pd.isna(project_feedback) else str(project_feedback).strip()
    design_feedback = "" if pd.isna(design_feedback) else str(design_feedback).strip()

    return {
        "project_id": project_id,
        "links_raw": links,
        "project_feedback": project_feedback,
        "design_feedback": design_feedback,
    }


async def main(csv_path: Path, output_path: Path) -> None:
    """Main extraction pipeline."""
    logger.info("Reading %s", csv_path)
    df = pd.read_csv(csv_path, quoting=1)  # QUOTE_ALL for proper multiline handling

    # Normalize column names (strip whitespace)
    df.columns = df.columns.str.strip()

    required_cols = ["Project ID", "links", "Feedback on project", "Feedback on design doc"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.error("Missing columns: %s. Available: %s", missing, list(df.columns))
        sys.exit(1)

    # Process all rows
    records = []
    for _, row in df.iterrows():
        rec = process_row(row)
        if rec:
            records.append(rec)

    logger.info("Processing %d projects", len(records))

    # Collect all unique URLs to fetch
    all_pr_urls = []
    all_wiki_urls = []
    pr_url_to_records = []  # list of (record_idx, url)
    wiki_url_to_records = []

    for idx, rec in enumerate(records):
        pr_urls = extract_github_pr_api_urls(rec["links_raw"])
        wiki_urls = extract_wiki_urls(rec["links_raw"])
        for url in pr_urls:
            all_pr_urls.append(url)
            pr_url_to_records.append((idx, url))
        for url in wiki_urls:
            all_wiki_urls.append(url)
            wiki_url_to_records.append((idx, url))

    # Deduplicate URLs for fetching (but track which records need them)
    unique_pr_urls = list(dict.fromkeys(all_pr_urls))
    unique_wiki_urls = list(dict.fromkeys(all_wiki_urls))

    logger.info("Found %d unique GitHub PR URLs, %d unique Wiki URLs", len(unique_pr_urls), len(unique_wiki_urls))

    # Fetch GitHub PRs via API (with diff Accept header) and Wiki URLs separately
    # GitHub API needs special headers; optional token for higher rate limits (60 vs 5000/hr)
    github_headers = {"Accept": GITHUB_DIFF_ACCEPT}
    if token := os.environ.get("GITHUB_TOKEN"):
        github_headers["Authorization"] = f"Bearer {token}"
    elif token := os.environ.get("GH_TOKEN"):
        github_headers["Authorization"] = f"Bearer {token}"

    fetched = {}
    if unique_pr_urls:
        pr_fetched = await fetch_urls(unique_pr_urls, extra_headers=github_headers)
        fetched.update(pr_fetched)
    if unique_wiki_urls:
        wiki_fetched = await fetch_urls(unique_wiki_urls)
        fetched.update(wiki_fetched)

    # Build content per record
    for rec in records:
        rec["diff"] = ""
        rec["wiki_content"] = ""

    # Assign fetched diffs to records
    for idx, url in pr_url_to_records:
        if url in fetched:
            content = fetched[url]
            if records[idx]["diff"]:
                records[idx]["diff"] += "\n\n---\n\n"
            records[idx]["diff"] += content

    # Assign fetched wiki content to records (parse HTML)
    for idx, url in wiki_url_to_records:
        if url in fetched:
            html = fetched[url]
            text = extract_main_content(html)
            if records[idx]["wiki_content"]:
                records[idx]["wiki_content"] += "\n\n---\n\n"
            records[idx]["wiki_content"] += text

    # Write output (remove intermediate fields)
    output_records = []
    for rec in records:
        output_records.append({
            "project_id": rec["project_id"],
            "diff": rec["diff"],
            "wiki_content": rec["wiki_content"],
            "project_feedback": rec["project_feedback"],
            "design_feedback": rec["design_feedback"],
        })

    with open(output_path, "w", encoding="utf-8") as f:
        for rec in output_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    logger.info("Wrote %d records to %s", len(output_records), output_path)


def run():
    parser = argparse.ArgumentParser(description="Extract PR and Wiki data from projects.csv")
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=Path("projects.csv"),
        help="Path to projects.csv",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("dataset_raw.jsonl"),
        help="Output JSONL file path",
    )
    args = parser.parse_args()

    if not args.input.exists():
        logger.error("Input file not found: %s", args.input)
        sys.exit(1)

    asyncio.run(main(args.input, args.output))


if __name__ == "__main__":
    run()
