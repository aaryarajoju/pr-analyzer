"""
cmo_detector.py  – Class Method Overuse detection.

Thin extraction layer over combined static-analysis findings.
"""

from __future__ import annotations

CMO_RATIO_THRESHOLD = 0.5   # Must match CMO_RATIO_THRESHOLD in static_analyzer.rb


def extract_cmo(findings: dict) -> dict:
    """Return the CMO sub-section from a combined findings dict."""
    return findings.get("cmo", {"violations": [], "count": 0})


def summarize_cmo(cmo: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = cmo.get("count", 0)
    if count == 0:
        return "CMO: no class-method-overuse violations detected."
    names = [v.get("class_name", "?") for v in cmo.get("violations", [])]
    return (
        f"CMO: {count} class(es) with high class-method ratio (>{int(CMO_RATIO_THRESHOLD * 100)}%): "
        + ", ".join(names[:5])
        + ("..." if len(names) > 5 else "")
        + "."
    )
