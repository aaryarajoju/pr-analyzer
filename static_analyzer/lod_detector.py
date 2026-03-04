"""
lod_detector.py  – Law of Demeter chain-depth detection.

Thin extraction layer: pulls LoD findings from a combined static-analysis result dict
or (for standalone use) runs the analyzers directly.
"""

from __future__ import annotations

LOD_MAX_CHAIN = 3  # Must match LOD_MAX_CHAIN in static_analyzer.rb and ts_static_analyzer.py


def extract_lod(findings: dict) -> dict:
    """
    Extract the LoD sub-section from a combined findings dict produced by run_all.
    Returns {"violations": [...], "count": N}.
    """
    return findings.get("lod", {"violations": [], "count": 0})


def summarize_lod(lod: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = lod.get("count", 0)
    if count == 0:
        return "LoD: no chain violations detected."
    violations = lod.get("violations", [])
    max_depth = max((v.get("chain_depth", 0) for v in violations), default=0)
    return (
        f"LoD: {count} method-chain violation(s) detected "
        f"(deepest chain: {max_depth} levels; threshold: {LOD_MAX_CHAIN})."
    )
