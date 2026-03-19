"""
static_analyzer – Hybrid static analysis module for pr-analyzer.

Architecture (NEW-DESIGN.md §7):
  1. ruby_static_analyzer.py  – Invokes parsers/static_analyzer.rb via subprocess
                                 on Ruby files; returns the full static-findings JSON.
  2. ts_static_analyzer.py    – Invokes parsers/ts_parser.ts (extended) + regex LoD
                                 on TypeScript/TSX files.
  3. run_all.py               – Orchestrates both analyzers over a list of
                                 {path, content} dicts (from extract_data_v2.py)
                                 and returns a combined findings dict.

The combined findings dict schema mirrors NEW-DESIGN.md §5.6:
  {
    "lod":  { "violations": [...], "count": N },
    "cmo":  { "violations": [...], "count": N },
    "srp":  { "signals":    [...], "count": N },
    "dry":  { "violations": [...], "count": N },
    "lsp":  { "signals":    [...], "count": N },
    "files_analyzed": N,
    "parse_errors":   [...]
  }
"""

from .run_all import run_static_analysis

__all__ = ["run_static_analysis"]
