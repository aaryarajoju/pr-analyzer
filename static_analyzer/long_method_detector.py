"""
long_method_detector.py  – Long Method detection.

Thin extraction layer over combined static-analysis findings.
The full implementation lives in parsers/static_analyzer.rb.
"""

from __future__ import annotations

LONG_METHOD_MAX_LINES = 20


def extract_long_method(findings: dict) -> dict:
    """Return the Long Method sub-section from a combined findings dict."""
    return findings.get("long_method", {"violations": [], "count": 0})


def summarize_long_method(long_method: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = long_method.get("count", 0)
    if count == 0:
        return "Long Method: no long-method violations detected."
    violations = long_method.get("violations", [])
    max_lines = max((v.get("line_count", 0) for v in violations), default=0)
    items = [
        f"`{v.get('class_name', '?')}#{v.get('method_name', '?')}` ({v.get('line_count', 0)} lines)"
        for v in violations[:3]
    ]
    suffix = "..." if count > 3 else ""
    return (
        f"Long Method: {count} method(s) with >= {LONG_METHOD_MAX_LINES} lines "
        f"(longest: {max_lines}): "
        + "; ".join(items)
        + suffix
        + "."
    )
