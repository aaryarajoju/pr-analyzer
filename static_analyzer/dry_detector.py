"""
dry_detector.py  – DRY (Don't Repeat Yourself) clone detection.

Thin extraction layer over combined static-analysis findings.
The full implementation (structural-hash–based deduplication) lives in parsers/static_analyzer.rb.

Future enhancement: integrate `flay` (Ruby gem) for deeper Type-1/2/3 clone detection
and `jscpd` (npm) for TypeScript. See NEW-DESIGN.md §5.3.
"""

from __future__ import annotations


def extract_dry(findings: dict) -> dict:
    """Return the DRY sub-section from a combined findings dict."""
    return findings.get("dry", {"violations": [], "count": 0})


def summarize_dry(dry: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = dry.get("count", 0)
    if count == 0:
        return "DRY: no duplicate-method bodies detected."
    violations = dry.get("violations", [])
    # De-duplicate: same structural_hash means same clone group; report unique method names
    seen_methods = set()
    unique_groups = 0
    for v in violations:
        key = v.get("method_name", "?")
        if key not in seen_methods:
            seen_methods.add(key)
            unique_groups += 1
    return (
        f"DRY: {count} duplicate method-body instance(s) across {unique_groups} unique method(s)."
    )
