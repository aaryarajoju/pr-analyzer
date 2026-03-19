"""
god_object_detector.py  – God Object detection.

Thin extraction layer over combined static-analysis findings.
The full implementation lives in parsers/static_analyzer.rb.
"""

from __future__ import annotations

GOD_OBJECT_MAX_METHODS = 15
GOD_OBJECT_MAX_IVARS  = 10
GOD_OBJECT_MAX_INITS  = 8


def extract_god_object(findings: dict) -> dict:
    """Return the God Object sub-section from a combined findings dict."""
    return findings.get("god_object", {"violations": [], "count": 0})


def summarize_god_object(god_object: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = god_object.get("count", 0)
    if count == 0:
        return "God Object: no god-object violations detected."
    violations = god_object.get("violations", [])
    names = [v.get("class_name", "?") for v in violations]
    return (
        f"God Object: {count} class(es) with too many methods/ivars/external instantiations "
        f"(thresholds: {GOD_OBJECT_MAX_METHODS} methods, {GOD_OBJECT_MAX_IVARS} ivars, {GOD_OBJECT_MAX_INITS} inits): "
        + ", ".join(names[:5])
        + ("..." if len(names) > 5 else "")
        + "."
    )
