#!/usr/bin/env python3
"""
Evaluate design principles (SRP, DRY, LoD, Class Method Overuse) using parsers and Ollama.

Reads dataset_raw.jsonl line by line, runs parsers on .rb/.ts/.tsx files found in diffs,
sends combined prompt to Ollama (DeepSeek), and appends results to evaluations.jsonl
immediately after each project to avoid data loss on crash.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests

from static_analyzer.run_all import run_static_analysis

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent
PARSERS_DIR = PROJECT_ROOT / "parsers"
CONTROLLER = PARSERS_DIR / "controller.py"

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "deepseek-coder-v2:16b-lite-instruct-q4_K_M"

# File type patterns
RB_PATTERN = re.compile(r"\.rb\b", re.IGNORECASE)
TS_PATTERN = re.compile(r"\.(?:tsx?|ts)\b", re.IGNORECASE)

# Diff parsing: extract file path from "diff --git a/path b/path" or "--- a/path" or "+++ b/path"
DIFF_GIT_PATTERN = re.compile(r"diff --git a/(.+?) b/\1")
DIFF_HEADER_PATTERN = re.compile(r"^(?:---|\+\+\+) [ab]/(.+)$", re.MULTILINE)


def parse_diff_file_sections(diff: str) -> list[tuple[str, str]]:
    """
    Parse a unified diff and return list of (file_path, extracted_content).
    Extracts added lines (+) and context lines (space) to approximate new file content.
    """
    if not diff or not diff.strip():
        return []

    sections: list[tuple[str, str]] = []
    path: Optional[str] = None
    content_lines: list[str] = []

    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            m = re.search(r"a/(.+?) b/\1", line)
            if m:
                if path and content_lines:
                    sections.append((path, "\n".join(content_lines)))
                path = m.group(1).strip()
                content_lines = []
        elif line.startswith("--- a/") or line.startswith("+++ b/"):
            m = re.match(r"^(?:---|\+\+\+) [ab]/(.+)$", line)
            if m:
                p = m.group(1).strip()
                if path and p != path and content_lines:
                    sections.append((path, "\n".join(content_lines)))
                    path = p
                    content_lines = []
                else:
                    path = p
        elif path:
            if line.startswith("+") and not line.startswith("+++"):
                content_lines.append(line[1:])
            elif line.startswith(" "):
                content_lines.append(line[1:])

    if path and content_lines:
        sections.append((path, "\n".join(content_lines)))

    return sections


def get_diff_file_paths(diff: str) -> list[str]:
    """Extract all file paths from a diff (from diff --git and ---/+++ headers)."""
    paths = set()
    for m in DIFF_GIT_PATTERN.finditer(diff):
        paths.add(m.group(1).strip())
    for m in DIFF_HEADER_PATTERN.finditer(diff):
        paths.add(m.group(1).strip())
    for m in re.finditer(r"diff --git a/(.+?) b/\1", diff):
        paths.add(m.group(1).strip())
    for m in re.finditer(r"^(?:---|\+\+\+) [ab]/(.+)$", diff, re.MULTILINE):
        paths.add(m.group(1).strip())
    return list(paths)


def has_rb_files(diff: str) -> bool:
    """Check if diff contains any .rb files."""
    for path in get_diff_file_paths(diff):
        if RB_PATTERN.search(path):
            return True
    return False


def has_ts_files(diff: str) -> bool:
    """Check if diff contains any .ts or .tsx files."""
    for path in get_diff_file_paths(diff):
        if re.search(r"\.(tsx?|ts)\b", path, re.IGNORECASE):
            return True
    return False


def run_parser(file_path: str, content: str, suffix: str) -> Optional[dict]:
    """Run the appropriate parser on content via a temp file. Returns parsed JSON or None."""
    suffix = suffix.lower()
    if suffix not in (".rb", ".ts", ".tsx"):
        return None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp_path = f.name
        try:
            result = subprocess.run(
                [sys.executable, str(CONTROLLER), tmp_path],
                capture_output=True,
                text=True,
                cwd=PROJECT_ROOT,
                env={**os.environ},
                timeout=30,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            # Diff fragments are often incomplete; parser failure is expected
            logger.debug("Parser failed for %s (incomplete fragment): %s", file_path, (result.stderr or "")[:300])
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        logger.debug("Parser error for %s: %s", file_path, e)
        return None


def run_parsers_for_diff(diff: str) -> dict:
    """
    Run Ruby and/or TypeScript parsers on files found in the diff.
    Returns combined parser output as a dict.
    """
    sections = parse_diff_file_sections(diff)
    rb_results: list[dict] = []
    ts_results: list[dict] = []

    for path, content in sections:
        if RB_PATTERN.search(path):
            out = run_parser(path, content, ".rb")
            if out:
                rb_results.append({"path": path, **out})
        elif re.search(r"\.(tsx?|ts)\b", path, re.IGNORECASE):
            ext = ".tsx" if ".tsx" in path.lower() else ".ts"
            out = run_parser(path, content, ext)
            if out:
                ts_results.append({"path": path, **out})

    result: dict = {}
    if rb_results:
        result["ruby"] = rb_results
    if ts_results:
        result["typescript"] = ts_results
    return result


def build_prompt(parser_json: dict, wiki_content: str, diff: str) -> str:
    """Build the evaluation prompt for DeepSeek."""
    parser_str = json.dumps(parser_json, indent=2) if parser_json else "{}"
    return f"""You are evaluating code design principles. Analyze the following and provide a concise evaluation for each principle.

## Parser Output (extracted structure from the code)
{parser_str}

## Wiki / Design Documentation
{wiki_content or "(none)"}

## Code Diff
{diff or "(empty)"}

---

Evaluate the code changes for these design principles:
1. **SRP (Single Responsibility Principle)** - Does each class/module have a single reason to change?
2. **DRY (Don't Repeat Yourself)** - Is there unnecessary duplication?
3. **LoD (Law of Demeter)** - Are there long chains of method calls on other objects?
4. **Class Method Overuse** - Are class methods used appropriately vs instance methods?

Provide a brief assessment for each principle (1-3 sentences) and an overall design quality score (1-5) with brief justification."""


def call_ollama(prompt: str, model: str = DEFAULT_MODEL, timeout: int = 120) -> str:
    """Send prompt to Ollama and return the generated response."""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "").strip()
    except requests.RequestException as e:
        logger.error("Ollama request failed: %s", e)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate design principles using parsers and Ollama"
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=PROJECT_ROOT / "dataset_raw.jsonl",
        help="Input JSONL file",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=PROJECT_ROOT / "evaluations.jsonl",
        help="Output JSONL file",
    )
    parser.add_argument(
        "--model",
        "-m",
        type=str,
        default=DEFAULT_MODEL,
        help="Ollama model name (default: deepseek)",
    )
    parser.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Number of lines to skip (for resuming)",
    )
    args = parser.parse_args()

    if not args.input.exists():
        logger.error("Input file not found: %s", args.input)
        sys.exit(1)

    output_path = args.output
    mode = "a" if output_path.exists() else "w"

    with open(args.input, "r", encoding="utf-8") as f_in:
        for line_num, line in enumerate(f_in, start=1):
            if args.skip and line_num <= args.skip:
                continue
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Invalid JSON at line %d: %s", line_num, e)
                continue

            project_id = record.get("project_id", f"line_{line_num}")
            diff = record.get("diff", "")
            wiki_content = record.get("wiki_content", "")

            logger.info("Processing project %s (line %d)", project_id, line_num)

            parser_output: dict = {}
            full_files = record.get("full_files")
            if full_files:
                # dataset_v2.jsonl: run full static analysis via static_analyzer package
                logger.info("Running static analysis on %d full file(s)", len(full_files))
                parser_output = run_static_analysis(full_files)
            elif diff and (has_rb_files(diff) or has_ts_files(diff)):
                # dataset_raw.jsonl: fall back to diff fragment analysis
                parser_output = run_parsers_for_diff(diff)
            else:
                logger.info("No .rb or .ts/.tsx files found, skipping parser")

            prompt = build_prompt(parser_output, wiki_content, diff)
            try:
                evaluation = call_ollama(prompt, model=args.model)
            except Exception as e:
                logger.error("Ollama failed for %s: %s", project_id, e)
                evaluation = f"Error: {e}"

            output_record = {
                "project_id": project_id,
                "parser_output": parser_output,
                "evaluation": evaluation,
            }
            with open(output_path, mode, encoding="utf-8") as f_out:
                f_out.write(json.dumps(output_record, ensure_ascii=False) + "\n")
            mode = "a"
            logger.info("Saved evaluation for %s", project_id)


if __name__ == "__main__":
    main()
