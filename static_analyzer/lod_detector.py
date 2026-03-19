"""
lod_detector.py  – Law of Demeter (true LoD) detection.

Thin extraction layer: pulls LoD findings from a combined static-analysis result dict.
True LoD = method accesses foreign object through intermediary (not self, params, ivar, etc).
"""

from __future__ import annotations

LOD_FOREIGN_DEPTH = 2  # Must match LOD_FOREIGN_DEPTH in static_analyzer.rb


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
        return "LoD: no true Law of Demeter violations detected."
    violations = lod.get("violations", [])
    max_depth = max((v.get("depth", v.get("chain_depth", 0)) for v in violations), default=0)
    return (
        f"LoD: {count} foreign-object chain violation(s) detected "
        f"(deepest: {max_depth} levels; root not self/params/ivar)."
    )
