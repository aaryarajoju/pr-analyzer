"""
lsp_detector.py  – Liskov Substitution Principle override-signal detection.

Thin extraction layer over combined static-analysis findings.
The full implementation (arity mismatch in overrides) lives in parsers/static_analyzer.rb.
LSP is considered LLM-primary; static findings here are candidate *signals* only.
"""

from __future__ import annotations


def extract_lsp(findings: dict) -> dict:
    """Return the LSP sub-section from a combined findings dict."""
    return findings.get("lsp", {"signals": [], "count": 0})


def summarize_lsp(lsp: dict) -> str:
    """One-line human summary for use in LLM prompts."""
    count = lsp.get("count", 0)
    if count == 0:
        return "LSP: no override-arity signals detected."
    signals = lsp.get("signals", [])
    items = [
        f"`{s.get('class_name', '?')}#{s.get('method_name', '?')}` "
        f"(arity {s.get('child_arity', '?')} vs parent {s.get('parent_arity', '?')})"
        for s in signals[:3]
    ]
    suffix = "..." if count > 3 else ""
    return f"LSP: {count} override signal(s) — " + "; ".join(items) + suffix + "."
