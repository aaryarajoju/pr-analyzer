"""
shotgun_surgery_detector.py  – Shotgun Surgery detection.

Thin extraction layer over combined static-analysis findings.
The full implementation lives in parsers/static_analyzer.rb.
"""

from __future__ import annotations

SHOTGUN_SURGERY_MIN_EXTERNAL_CLASSES = 8


def extract_shotgun_surgery(findings: dict) -> dict:
    """Return the Shotgun Surgery sub-section from a combined findings dict."""
    return findings.get("shotgun_surgery", {"violations": [], "count": 0})


def summarize_shotgun_surgery(shotgun_surgery: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = shotgun_surgery.get("count", 0)
    if count == 0:
        return "Shotgun Surgery: no shotgun-surgery signals detected."
    violations = shotgun_surgery.get("violations", [])
    items = [
        f"{v.get('file', '?')} ({v.get('external_class_count', 0)} external classes)"
        for v in violations[:3]
    ]
    suffix = "..." if count > 3 else ""
    return (
        f"Shotgun Surgery: {count} file(s) with >= {SHOTGUN_SURGERY_MIN_EXTERNAL_CLASSES} external class refs: "
        + "; ".join(items)
        + suffix
        + "."
    )
