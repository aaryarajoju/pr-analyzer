"""
ocp_detector.py  – Open/Closed Principle detection.

Thin extraction layer over combined static-analysis findings.
The full implementation lives in parsers/static_analyzer.rb.
"""

from __future__ import annotations

OCP_MIN_BRANCHES    = 4
OCP_TYPE_CHECK_MIN  = 2


def extract_ocp(findings: dict) -> dict:
    """Return the OCP sub-section from a combined findings dict."""
    return findings.get("ocp", {"violations": [], "count": 0})


def summarize_ocp(ocp: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = ocp.get("count", 0)
    if count == 0:
        return "OCP: no open/closed violations detected."
    violations = ocp.get("violations", [])
    items = [
        f"`{v.get('class_name', '?')}#{v.get('method_name', '?')}` "
        f"({v.get('branch_count', 0)} branches, {v.get('type_checks', 0)} type checks)"
        for v in violations[:3]
    ]
    suffix = "..." if count > 3 else ""
    return (
        f"OCP: {count} method(s) with type-checking branches (min {OCP_MIN_BRANCHES} branches, "
        f"{OCP_TYPE_CHECK_MIN} type checks): "
        + "; ".join(items)
        + suffix
        + "."
    )
