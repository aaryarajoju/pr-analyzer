#!/usr/bin/env python3
"""
evaluate_design_hybrid.py  – LLM-Primary Design Violation Evaluator.

The LLM finds and counts violations (thesis goal). Static analysis runs for
reference/comparison but is not used for output.

  1. Read dataset_v2.jsonl (produced by extract_data_v2.py).
  2. For each project:
       a. Run static analysis (for reference; stored in static_findings).
       b. Chunk code into batches (50K chars each) so LLM sees more.
       c. Call Ollama per batch; sum violation counts across batches.
       d. Write result to evaluations_hybrid.jsonl immediately (crash-safe).

Output record schema (same as analyze_violations.py for compatibility):
  {
    "project_id":      "E2541",
    "static_findings": { "lod": {...}, "cmo": {...}, "srp": {...}, "dry": {...}, "lsp": {...} },
    "violations": {
        "SRP": N, "DRY": N, "LoD": N, "ClassMethodOveruse": N, "LSP": N
    },
    "total_violations": N,
    "confidence":       N,   # 1–5
    "summary":          "..."
  }

Usage:
    python evaluate_design_hybrid.py --input dataset_v2.jsonl --output evaluations_hybrid.jsonl
    python evaluate_design_hybrid.py --input dataset_raw.jsonl  # diff-only fallback (no full files)
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

import requests

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
# Paths & defaults
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
OLLAMA_URL      = "http://localhost:11434/api/generate"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
DEFAULT_MODEL   = "deepseek-coder-v2:16b-lite-instruct-q4_K_M"

# Maximum characters of code per batch (increased so LLM sees more; many models support 32K+ tokens)
MAX_CODE_CHARS = 50_000
MAX_DIFF_CHARS = 10_000

# ─────────────────────────────────────────────────────────────────────────────
# LLM-as-primary prompt: the LLM finds and counts violations (thesis goal).
# Static analysis is optional reference only.
# ─────────────────────────────────────────────────────────────────────────────
LLM_COUNT_PROMPT = """\
You are a strict code reviewer. Your job is to COUNT design principle violations in the code below.

## Code to analyze
{code}

## Design principles (count each occurrence)
- **LoD (Law of Demeter)**: Method chains > 4 levels (e.g. obj.a.b.c.d.e). Count each chain.
- **SRP**: Classes with > 7 methods or multiple unrelated responsibilities. Count each class.
- **DRY**: Duplicate/similar logic. Count each duplication.
- **ClassMethodOveruse**: Classes where > 50% of methods are class methods. Count each class.
- **LSP**: Override with different arity than parent. Count each override.

Be thorough. Count every violation you see. Do not be lenient.

Return ONLY valid JSON (no markdown):
{{
  "violations": {{"SRP": N, "DRY": N, "LoD": N, "ClassMethodOveruse": N, "LSP": N}},
  "total_violations": N,
  "confidence": 1-5,
  "summary": "1-2 sentence summary of main issues"
}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Ollama helpers (shared logic kept consistent with analyze_violations.py)
# ─────────────────────────────────────────────────────────────────────────────
MODEL_ALIASES: dict[str, str] = {
    "deepseek": "deepseek-coder-v2:16b-lite-instruct-q4_K_M",
}


def check_ollama(model: str) -> None:
    try:
        r = requests.get(OLLAMA_TAGS_URL, timeout=5)
        r.raise_for_status()
        all_names = [m["name"] for m in r.json().get("models", [])]
        found = model in all_names or any(n.startswith(model) or model in n for n in all_names)
        if not found:
            avail = ", ".join(all_names[:5]) or "(none)"
            logger.error('Model "%s" not found. Available: %s. Run: ollama pull %s', model, avail, model)
            sys.exit(1)
    except requests.RequestException:
        logger.error("Cannot reach Ollama at localhost:11434. Start it with: ollama serve")
        sys.exit(1)


def call_ollama(prompt: str, model: str = DEFAULT_MODEL, timeout: int = 120) -> str:
    resp = requests.post(
        OLLAMA_URL,
        json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def parse_llm_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Static analysis import (lazy to avoid import errors if Ruby is unavailable)
# ─────────────────────────────────────────────────────────────────────────────
def _run_static_analysis(full_files: list[dict]) -> dict:
    """Run static analysis; return empty findings on ImportError or failure."""
    try:
        from static_analyzer import run_static_analysis
        return run_static_analysis(full_files)
    except ImportError:
        logger.warning("static_analyzer module not found; skipping static analysis.")
        return {}
    except Exception as exc:
        logger.warning("Static analysis failed: %s", exc)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Prompt building
# ─────────────────────────────────────────────────────────────────────────────
def _build_static_summary(findings: dict) -> str:
    """
    Build the static-summary section of the hybrid prompt.
    Uses the pre-computed human summaries if present, otherwise falls back to
    the raw counts.
    """
    if not findings:
        return "(no static analysis was performed – analyze based on code only)"

    summaries = findings.get("summaries", {})
    if summaries:
        lines = [
            f"- {summaries.get('srp', 'SRP: N/A')}",
            f"- {summaries.get('dry', 'DRY: N/A')}",
            f"- {summaries.get('lod', 'LoD: N/A')}",
            f"- {summaries.get('cmo', 'CMO: N/A')}",
            f"- {summaries.get('lsp', 'LSP: N/A')}",
        ]
    else:
        # Fallback: raw counts
        lines = []
        for key, sub_key, label in [
            ("lod",  "violations", "LoD"),
            ("cmo",  "violations", "CMO (Class Method Overuse)"),
            ("srp",  "signals",    "SRP"),
            ("dry",  "violations", "DRY"),
            ("lsp",  "signals",    "LSP"),
        ]:
            sec   = findings.get(key, {})
            count = sec.get("count", 0)
            lines.append(f"- {label}: {count} finding(s) detected")

    files_n = findings.get("files_analyzed", 0)
    header  = f"(Static analysis ran on {files_n} file(s))\n" if files_n else ""

    # Include top violations for context (first 3 of each)
    details = []
    for key, sub_key in [("lod", "violations"), ("dry", "violations"), ("srp", "signals"), ("lsp", "signals")]:
        items = findings.get(key, {}).get(sub_key, [])[:3]
        for item in items:
            desc = item.get("description", "")
            if desc:
                details.append(f"  • {desc}")

    body = "\n".join(lines)
    if details:
        body += "\n\nTop findings:\n" + "\n".join(details)

    return header + body


def _build_code_batches(full_files: list[dict], diff: str, max_chars: int = MAX_CODE_CHARS) -> list[str]:
    """
    Split code into batches, each under max_chars. Enables chunked LLM analysis
    so the model sees more code across multiple calls.
    """
    batches: list[str] = []
    current: list[str] = []
    current_len = 0

    if full_files:
        for f in full_files:
            path = f.get("path", "unknown")
            content = f.get("content", "")
            chunk = f"### {path}\n```\n{content}\n```"
            if current_len + len(chunk) > max_chars and current:
                batches.append("\n\n".join(current))
                current = []
                current_len = 0
            if len(chunk) > max_chars:
                chunk = f"### {path}\n```\n{content[:max_chars - 100]}\n... (truncated)\n```"
            current.append(chunk)
            current_len += len(chunk)
        if current:
            batches.append("\n\n".join(current))
    elif diff:
        truncated = diff[:max_chars]
        if len(diff) > max_chars:
            truncated += "\n\n... (diff truncated)"
        batches.append(f"### Diff\n```diff\n{truncated}\n```")

    return batches if batches else ["(no code or diff available)"]


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────
def process_record(record: dict, model: str, max_chars: int = MAX_CODE_CHARS) -> dict:
    project_id  = record.get("project_id", "unknown")
    diff        = record.get("diff", "")
    wiki        = record.get("wiki_content", "")
    full_files  = record.get("full_files", [])

    logger.info("Analyzing %s (%d full file(s))", project_id, len(full_files))

    # Step 1: Static analysis (for reference/comparison; LLM is primary for thesis)
    static_findings = _run_static_analysis(full_files) if full_files else {}

    # Step 2: Chunked LLM analysis – send code in batches, sum counts (LLM is primary)
    batches = _build_code_batches(full_files, diff, max_chars=max_chars)
    if len(batches) > 1:
        logger.info("  %s: %d code batch(es) for LLM", project_id, len(batches))
    accumulated = {"SRP": 0, "DRY": 0, "LoD": 0, "ClassMethodOveruse": 0, "LSP": 0}
    summary = ""
    confidence = 0

    for i, code_batch in enumerate(batches):
        prompt = LLM_COUNT_PROMPT.format(code=code_batch)
        try:
            raw = call_ollama(prompt, model=model)
            data = parse_llm_json(raw)
            v = data.get("violations", {})
            for key in accumulated:
                accumulated[key] += v.get(key, 0)
            if i == len(batches) - 1:
                summary = data.get("summary", "")
                confidence = data.get("confidence", 0)
        except Exception as exc:
            logger.error("Ollama failed for %s batch %d: %s", project_id, i + 1, exc)

    total = sum(accumulated.values())

    return {
        "project_id":      project_id,
        "static_findings": {
            k: v2
            for k, v2 in static_findings.items()
            if k not in ("summaries",)
        },
        "violations":       accumulated,
        "total_violations": total,
        "confidence":       confidence,
        "summary":          summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hybrid static + LLM design principle evaluator."
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        default=PROJECT_ROOT / "dataset_v2.jsonl",
        help="Input JSONL (dataset_v2.jsonl with full_files, or dataset_raw.jsonl as fallback).",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=PROJECT_ROOT / "evaluations_hybrid.jsonl",
        help="Output JSONL file.",
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default=DEFAULT_MODEL,
        help="Ollama model name.",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Only process first N records.",
    )
    parser.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Skip first N lines (for resuming interrupted runs).",
    )
    parser.add_argument(
        "--no-static",
        action="store_true",
        help="Disable static analysis; use LLM-only mode.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=MAX_CODE_CHARS,
        help=f"Max chars per code batch (default {MAX_CODE_CHARS}). Lower if model has small context.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        logger.error("Input not found: %s", args.input)
        sys.exit(1)

    model = MODEL_ALIASES.get(args.model, args.model)
    check_ollama(model)

    mode = "a" if args.output.exists() else "w"

    processed = 0
    with open(args.input, "r", encoding="utf-8") as f_in:
        for line_num, line in enumerate(f_in, start=1):
            if args.skip and line_num <= args.skip:
                continue
            if args.limit and processed >= args.limit:
                break

            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid JSON at line %d: %s", line_num, exc)
                continue

            # In --no-static mode strip full_files so static analysis is skipped
            if args.no_static:
                record.pop("full_files", None)

            result = process_record(record, model=model, max_chars=args.max_chars)

            with open(args.output, mode, encoding="utf-8") as f_out:
                f_out.write(json.dumps(result, ensure_ascii=False) + "\n")
            mode = "a"

            processed += 1
            logger.info(
                "Saved %s  → total_violations=%d  confidence=%s",
                result["project_id"],
                result["total_violations"],
                result["confidence"],
            )

    logger.info("Done. Processed %d record(s) → %s", processed, args.output)


if __name__ == "__main__":
    main()
