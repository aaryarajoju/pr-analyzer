"""
srp_detector.py  – Single Responsibility Principle heuristics.

Thin extraction layer over combined static-analysis findings.
The full heuristic implementation lives in parsers/static_analyzer.rb.
Python callers use this module to extract + summarize SRP signals.
"""

from __future__ import annotations

SRP_MAX_METHODS = 7   # Must match SRP_MAX_METHODS in static_analyzer.rb


def extract_srp(findings: dict) -> dict:
    """Return the SRP sub-section from a combined findings dict."""
    return findings.get("srp", {"signals": [], "count": 0})


def summarize_srp(srp: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = srp.get("count", 0)
    if count == 0:
        return "SRP: no god-class signals detected."
    signals = srp.get("signals", [])
    names = [s.get("class_name", "?") for s in signals]
    max_methods = max((s.get("method_count", 0) for s in signals), default=0)
    return (
        f"SRP: {count} class(es) with possible SRP violations "
        f"(largest: {max_methods} methods; threshold: {SRP_MAX_METHODS}): "
        + ", ".join(names[:5])
        + ("..." if len(names) > 5 else "")
        + "."
    )
