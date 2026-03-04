"""
ts_static_analyzer.py

Static analysis for TypeScript / TSX files.

Strategy (per NEW-DESIGN.md §5.1, §5.2):
  - LoD: Regex-based chain-length detection (same approach as the Ruby TextMetrics helper).
    TypeScript code often has patterns like:  `a.b.c.d()` or `response.data.items[0].name`.
  - CMO: Use existing ts_parser.ts output (which already counts hooks, components, imports).
    We flag files/classes with a high ratio of static-like top-level functions vs class members.
  - SRP / DRY / LSP: Not yet implemented for TypeScript — deferred to LLM validation.

Returns a partial findings dict (only lod + cmo populated) that is merged by run_all.py.
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MODULE_DIR   = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
_PARSERS_DIR  = _PROJECT_ROOT / "parsers"
_TS_PARSER    = _PARSERS_DIR / "ts_parser.ts"

# LoD threshold (mirrors LOD_MAX_CHAIN in static_analyzer.rb)
LOD_MAX_CHAIN = 3


def _lod_from_source(source: str, file_path: str) -> list[dict]:
    """
    Scan TypeScript / TSX source for method-call chains deeper than LOD_MAX_CHAIN.
    Uses a simple regex, identical in spirit to TextMetrics.call_chain_lengths in Ruby.
    """
    violations = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        # Match: identifier.identifier.identifier... (with optional ()[] at the end)
        # We ignore chains that start with capital letters (type paths like Foo.Bar.Baz)
        matches = re.findall(
            r"(?:[a-z_$][a-zA-Z0-9_$]*\.){2,}[a-zA-Z_$][a-zA-Z0-9_$]*",
            line,
        )
        for m in matches:
            depth = m.count(".") + 1
            if depth >= LOD_MAX_CHAIN:
                violations.append({
                    "file":        file_path,
                    "line":        lineno,
                    "chain":       m.strip(),
                    "chain_depth": depth,
                    "description": f"Call chain of depth {depth} at line {lineno} in {file_path}.",
                })
    return violations


def _run_ts_parser(path: str, content: str) -> Optional[dict]:
    """Run parsers/ts_parser.ts and return the parsed JSON, or None on failure."""
    suffix = ".tsx" if path.lower().endswith(".tsx") else ".ts"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp_path = f.name

        try:
            env = {**os.environ, "NODE_OPTIONS": "--no-warnings"}
            result = subprocess.run(
                ["npx", "tsx", str(_TS_PARSER), tmp_path],
                capture_output=True,
                text=True,
                cwd=_PARSERS_DIR,
                env=env,
                timeout=30,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            logger.debug("ts_parser failed for %s: %s", path, (result.stderr or "")[:200])
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:
        logger.debug("ts_static_analyzer error for %s: %s", path, exc)
        return None


def _cmo_from_ts_output(ts_output: dict, file_path: str) -> list[dict]:
    """
    Heuristic CMO detection from ts_parser.ts output.
    Flag when a file has many hooks/top-level functions but no class structure —
    an indicator of utility-namespace overuse.
    """
    violations = []
    hooks = ts_output.get("hooks", [])
    components = ts_output.get("components", [])

    # Simple heuristic: if hooks count is high and there are no component classes
    if len(hooks) >= 4 and not components:
        violations.append({
            "file":        file_path,
            "hook_count":  len(hooks),
            "component_count": 0,
            "description": (
                f"{file_path} defines {len(hooks)} hooks with no enclosing React component class; "
                "consider consolidating into a class or organized module."
            ),
        })

    return violations


def analyze_ts_files(files: list[dict]) -> dict:
    """
    Run TypeScript static analysis on one or more TS/TSX files.

    Parameters
    ----------
    files : list of {"path": str, "content": str}

    Returns
    -------
    dict with keys: lod, cmo  (partial findings — srp/dry/lsp left empty for LLM)
    """
    lod_violations: list[dict] = []
    cmo_violations: list[dict] = []

    for f in files:
        path    = f.get("path", "unknown.ts")
        content = f.get("content", "")
        if not content:
            continue

        # LoD: regex scan
        lod_violations.extend(_lod_from_source(content, path))

        # CMO + richer parser info
        ts_out = _run_ts_parser(path, content)
        if ts_out:
            cmo_violations.extend(_cmo_from_ts_output(ts_out, path))

    return {
        "lod": {"violations": lod_violations, "count": len(lod_violations)},
        "cmo": {"violations": cmo_violations, "count": len(cmo_violations)},
    }
