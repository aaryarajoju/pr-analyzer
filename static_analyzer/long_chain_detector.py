"""
long_chain_detector.py  – Long method chain (structural) detection.

Thin extraction layer: pulls long_chain findings from a combined static-analysis result dict.
Flags chains >= 5 deep regardless of ownership — fragile, hard to read code.
"""

from __future__ import annotations

LONG_CHAIN_MIN_DEPTH = 5  # Must match LONG_CHAIN_MIN_DEPTH in static_analyzer.rb


def extract_long_chain(findings: dict) -> dict:
    """
    Extract the long_chain sub-section from a combined findings dict produced by run_all.
    Returns {"violations": [...], "count": N}.
    """
    return findings.get("long_chain", {"violations": [], "count": 0})


def summarize_long_chain(long_chain: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = long_chain.get("count", 0)
    if count == 0:
        return "Long Chain: no long chain violations detected."
    violations = long_chain.get("violations", [])
    max_depth = max((v.get("depth", v.get("chain_depth", 0)) for v in violations), default=0)
    return (
        f"Long Chain: {count} chain(s) with >= {LONG_CHAIN_MIN_DEPTH} levels "
        f"(longest: {max_depth})."
    )
