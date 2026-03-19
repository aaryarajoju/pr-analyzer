"""
feature_envy_detector.py  – Feature Envy detection.

Thin extraction layer over combined static-analysis findings.
The full implementation lives in parsers/static_analyzer.rb.
"""

from __future__ import annotations

FEATURE_ENVY_MIN_EXTERNAL = 6  # Must match static_analyzer.rb; Controller/Helper/Migration excluded


def extract_feature_envy(findings: dict) -> dict:
    """Return the Feature Envy sub-section from a combined findings dict."""
    return findings.get("feature_envy", {"violations": [], "count": 0})


def summarize_feature_envy(feature_envy: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = feature_envy.get("count", 0)
    if count == 0:
        return "Feature Envy: no feature-envy violations detected."
    violations = feature_envy.get("violations", [])
    items = [
        f"`{v.get('class_name', '?')}#{v.get('method_name', '?')}` "
        f"({v.get('external_references', 0)} external vs {v.get('own_references', 0)} own)"
        for v in violations[:3]
    ]
    suffix = "..." if count > 3 else ""
    return (
        f"Feature Envy: {count} method(s) with more external refs than own (min {FEATURE_ENVY_MIN_EXTERNAL}): "
        + "; ".join(items)
        + suffix
        + "."
    )
