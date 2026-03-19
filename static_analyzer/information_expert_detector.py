"""
information_expert_detector.py  – Information Expert detection.

Thin extraction layer over combined static-analysis findings.
The full implementation lives in parsers/static_analyzer.rb.
"""

from __future__ import annotations

INFO_EXPERT_MIN_EXTERNAL = 8  # Must match INFO_EXPERT_MIN_EXTERNAL in static_analyzer.rb


def extract_information_expert(findings: dict) -> dict:
    """Return the Information Expert sub-section from a combined findings dict."""
    return findings.get("information_expert", {"violations": [], "count": 0})


def summarize_information_expert(information_expert: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = information_expert.get("count", 0)
    if count == 0:
        return "Information Expert: no violations detected."
    violations = information_expert.get("violations", [])
    items = [
        f"`{v.get('class_name', '?')}#{v.get('method_name', '?')}` "
        f"({v.get('external_calls', 0)} external vs {v.get('ivar_accesses', 0)} ivar)"
        for v in violations[:3]
    ]
    suffix = "..." if count > 3 else ""
    return (
        f"Information Expert: {count} method(s) with more external calls than ivar accesses "
        f"(min {INFO_EXPERT_MIN_EXTERNAL} external): "
        + "; ".join(items)
        + suffix
        + "."
    )
