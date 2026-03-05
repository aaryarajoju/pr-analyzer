"""
run_all.py  – Orchestrates static analysis across Ruby and TypeScript files.

Entry point for the hybrid pipeline:
  1. Separate input files by language.
  2. Run Ruby static analyzer (SRP, LoD, CMO, DRY, LSP).
  3. Run TypeScript static analyzer (LoD, CMO heuristics).
  4. Merge results into a single findings dict.
  5. Produce human-readable summaries for each detector (used in LLM prompts).

The returned dict schema matches NEW-DESIGN.md §5.6:
  {
    "lod":  { "violations": [...], "count": N },
    "cmo":  { "violations": [...], "count": N },
    "srp":  { "signals":    [...], "count": N },
    "dry":  { "violations": [...], "count": N },
    "lsp":  { "signals":    [...], "count": N },
    "files_analyzed": N,
    "parse_errors":   [...],
    "summaries": {            # One-liners for LLM prompt construction
        "lod": "...",
        "cmo": "...",
        "srp": "...",
        "dry": "...",
        "lsp": "..."
    }
  }

Usage (standalone):
    python -m static_analyzer.run_all --files files.json --output findings.json

    Where files.json is: [{"path": "...", "content": "..."}, ...]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .ruby_static_analyzer import analyze_ruby_files
from .ts_static_analyzer import analyze_ts_files
from .lod_detector import extract_lod, summarize_lod
from .cmo_detector import extract_cmo, summarize_cmo
from .srp_detector import extract_srp, summarize_srp
from .dry_detector import extract_dry, summarize_dry
from .lsp_detector import extract_lsp, summarize_lsp

logger = logging.getLogger(__name__)

# File extensions routed to each analyzer
RUBY_EXTS = {".rb"}
TS_EXTS   = {".ts", ".tsx"}


def _partition_files(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split input files into Ruby and TypeScript buckets."""
    ruby, typescript = [], []
    for f in files:
        path = (f.get("path") or "").lower()
        if any(path.endswith(ext) for ext in RUBY_EXTS):
            ruby.append(f)
        elif any(path.endswith(ext) for ext in TS_EXTS):
            typescript.append(f)
        # Other file types are ignored for static analysis
    return ruby, typescript


def _merge_findings(ruby_findings: dict, ts_findings: dict) -> dict:
    """
    Merge Ruby and TypeScript findings into a single combined dict.
    For sections that exist in both (lod, cmo), concatenate violation / signal lists.
    For sections only in Ruby (srp, dry, lsp), pass through unchanged.
    """
    def merge_section(key: str, sub_key: str) -> dict:
        ruby_section = ruby_findings.get(key, {})
        ts_section   = ts_findings.get(key, {})

        ruby_items = ruby_section.get(sub_key, [])
        ts_items   = ts_section.get(sub_key, [])
        combined   = ruby_items + ts_items

        return {sub_key: combined, "count": len(combined)}

    merged = {
        "lod": merge_section("lod", "violations"),
        "cmo": merge_section("cmo", "violations"),
        "srp": {"signals": ruby_findings.get("srp", {}).get("signals", []),
                "count":   ruby_findings.get("srp", {}).get("count", 0)},
        "dry": {"violations": ruby_findings.get("dry", {}).get("violations", []),
                "count":       ruby_findings.get("dry", {}).get("count", 0)},
        "lsp": {"signals": ruby_findings.get("lsp", {}).get("signals", []),
                "count":   ruby_findings.get("lsp", {}).get("count", 0)},
    }

    # Propagate parse errors
    errors = ruby_findings.get("parse_errors", []) + ts_findings.get("parse_errors", [])
    if errors:
        merged["parse_errors"] = errors

    return merged


def run_static_analysis(
    files: list[dict],
    ruby_timeout: int = 60,
) -> dict:
    """
    Main entry point.

    Parameters
    ----------
    files : list of {"path": str, "content": str}
        Full-file content for each changed file (from extract_data_v2.py).
    ruby_timeout : int
        Timeout for the Ruby subprocess in seconds.

    Returns
    -------
    dict
        Combined findings dict with a "summaries" key added for LLM prompt use.
    """
    if not files:
        logger.debug("run_static_analysis called with no files – returning empty findings.")
        return _make_empty_with_summaries()

    ruby_files, ts_files = _partition_files(files)

    logger.debug(
        "Static analysis: %d Ruby file(s), %d TS/TSX file(s)",
        len(ruby_files), len(ts_files),
    )

    # Run language-specific analyzers
    ruby_findings = analyze_ruby_files(ruby_files, timeout=ruby_timeout) if ruby_files else {}
    ts_findings   = analyze_ts_files(ts_files)                           if ts_files   else {}

    # Merge
    combined = _merge_findings(ruby_findings, ts_findings)
    combined["files_analyzed"] = len(ruby_files) + len(ts_files)

    # Attach human summaries for LLM prompt construction
    combined["summaries"] = {
        "lod": summarize_lod(extract_lod(combined)),
        "cmo": summarize_cmo(extract_cmo(combined)),
        "srp": summarize_srp(extract_srp(combined)),
        "dry": summarize_dry(extract_dry(combined)),
        "lsp": summarize_lsp(extract_lsp(combined)),
    }

    return combined


def _make_empty_with_summaries() -> dict:
    empty = {
        "lod": {"violations": [], "count": 0},
        "cmo": {"violations": [], "count": 0},
        "srp": {"signals":    [], "count": 0},
        "dry": {"violations": [], "count": 0},
        "lsp": {"signals":    [], "count": 0},
        "files_analyzed": 0,
    }
    empty["summaries"] = {
        "lod": "LoD: no files to analyze.",
        "cmo": "CMO: no files to analyze.",
        "srp": "SRP: no files to analyze.",
        "dry": "DRY: no files to analyze.",
        "lsp": "LSP: no files to analyze.",
    }
    return empty


# ─────────────────────────────────────────────────────────────────────────────
# CLI for standalone use / debugging
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    parser = argparse.ArgumentParser(
        description="Run static analysis on a set of files (JSON input).",
    )
    parser.add_argument(
        "--files", "-f",
        type=Path,
        required=True,
        help='Path to JSON file: [{"path": "...", "content": "..."}]',
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSON file (default: stdout).",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=60,
        help="Ruby subprocess timeout in seconds (default: 60).",
    )
    args = parser.parse_args()

    if not args.files.exists():
        logger.error("Files JSON not found: %s", args.files)
        sys.exit(1)

    files = json.loads(args.files.read_text(encoding="utf-8"))

    findings = run_static_analysis(files, ruby_timeout=args.timeout)

    output_text = json.dumps(findings, indent=2, ensure_ascii=False)

    if args.output:
        args.output.write_text(output_text, encoding="utf-8")
        logger.info("Wrote findings to %s", args.output)
    else:
        print(output_text)


if __name__ == "__main__":
    main()
