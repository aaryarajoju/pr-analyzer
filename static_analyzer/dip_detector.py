"""
dip_detector.py  – Dependency Inversion Principle detection.

Thin extraction layer over combined static-analysis findings.
The full implementation lives in parsers/static_analyzer.rb.
"""

from __future__ import annotations

DIP_MAX_CONCRETIONS = 2  # Must match DIP_MAX_CONCRETIONS in static_analyzer.rb


def extract_dip(findings: dict) -> dict:
    """Return the DIP sub-section from a combined findings dict."""
    return findings.get("dip", {"violations": [], "count": 0})


def summarize_dip(dip: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = dip.get("count", 0)
    if count == 0:
        return "DIP: no dependency-inversion violations detected."
    violations = dip.get("violations", [])
    names = [v.get("class_name", "?") for v in violations]
    max_concretions = max((v.get("concretion_count", 0) for v in violations), default=0)
    return (
        f"DIP: {count} class(es) with direct concrete instantiations "
        f"(max {max_concretions}; threshold: {DIP_MAX_CONCRETIONS}): "
        + ", ".join(names[:5])
        + ("..." if len(names) > 5 else "")
        + "."
    )
