"""
ruby_static_analyzer.py

Python wrapper around parsers/static_analyzer.rb.

Responsibility:
  - Write file content to temp files (so the Ruby script can read full files)
  - Invoke `bundle exec ruby static_analyzer.rb` via subprocess
  - Parse and return JSON findings

The Ruby script uses Prism (already in parsers/Gemfile) to run five detectors:
  SRP heuristics, LoD chain depth, CMO ratio, DRY structural-hash clones, LSP arity signals.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resolved at import time so callers don't need to worry about CWD
_MODULE_DIR  = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
_PARSERS_DIR  = _PROJECT_ROOT / "parsers"
_ANALYZER_RB  = _PARSERS_DIR / "static_analyzer.rb"

# Empty template returned when analysis produces no results
_EMPTY_FINDINGS: dict = {
    "lod": {"violations": [], "count": 0},
    "long_chain": {"violations": [], "count": 0},
    "cmo": {"violations": [], "count": 0},
    "srp": {"signals":    [], "count": 0},
    "dry": {"violations": [], "count": 0},
    "lsp": {"signals":    [], "count": 0},
    "god_object":      {"violations": [], "count": 0},
    "feature_envy":    {"violations": [], "count": 0},
    "long_method":     {"violations": [], "count": 0},
    "shotgun_surgery": {"violations": [], "count": 0},
    "ocp":             {"violations": [], "count": 0},
    "dip":             {"violations": [], "count": 0},
    "information_expert": {"violations": [], "count": 0},
}


def analyze_ruby_files(
    files: list[dict],
    timeout: int = 60,
) -> dict:
    """
    Run the Ruby static analyzer on one or more Ruby files.

    Parameters
    ----------
    files : list of {"path": str, "content": str}
        Each dict represents one Ruby file.  If ``content`` is given, it is
        written to a temp file so the analyzer sees the *full* file (not just
        a diff fragment).  If ``content`` is empty/None, the path is used as-is
        (must exist on disk).
    timeout : int
        Subprocess timeout in seconds.

    Returns
    -------
    dict
        Static findings in the NEW-DESIGN.md §5.6 schema.
    """
    if not files:
        return dict(_EMPTY_FINDINGS)

    if not _ANALYZER_RB.exists():
        logger.error(
            "static_analyzer.rb not found at %s. "
            "Ensure parsers/static_analyzer.rb exists.",
            _ANALYZER_RB,
        )
        return dict(_EMPTY_FINDINGS)

    # Write content to temp files; track (original_path, tmp_path) pairs
    tmp_files: list[tuple[str, str]] = []

    try:
        for f in files:
            original_path = f.get("path", "unknown.rb")
            content       = f.get("content") or ""

            if content:
                # Write to a named temp file with a .rb suffix
                tmp = tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".rb",
                    delete=False,
                    encoding="utf-8",
                    errors="replace",
                )
                tmp.write(content)
                tmp.flush()
                tmp.close()
                tmp_files.append((original_path, tmp.name))
            elif original_path and Path(original_path).exists():
                # Use the file on disk directly (no temp copy needed)
                tmp_files.append((original_path, original_path))
            else:
                logger.debug("Skipping %s: no content and file not found on disk", original_path)

        if not tmp_files:
            return dict(_EMPTY_FINDINGS)

        # Build the command
        ruby_paths = [t[1] for t in tmp_files]
        env = {**os.environ}

        cmd = ["ruby", str(_ANALYZER_RB), *ruby_paths]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=_PARSERS_DIR,
            env=env,
            timeout=timeout,
        )

        if result.returncode != 0:
            stderr_preview = (result.stderr or "")[:500]
            logger.warning(
                "static_analyzer.rb exited %d: %s",
                result.returncode,
                stderr_preview,
            )
            return dict(_EMPTY_FINDINGS)

        findings = json.loads(result.stdout)
        findings = _normalize_schema(findings)

        # Re-map temp file paths back to original logical paths
        path_map = {t[1]: t[0] for t in tmp_files}
        findings = _remap_paths(findings, path_map)

        return findings

    except subprocess.TimeoutExpired:
        logger.warning("Ruby static analyzer timed out after %ds", timeout)
        return dict(_EMPTY_FINDINGS)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse static_analyzer.rb output: %s", exc)
        return dict(_EMPTY_FINDINGS)
    except Exception as exc:
        logger.warning("Ruby static analyzer error: %s", exc)
        return dict(_EMPTY_FINDINGS)
    finally:
        # Clean up temp files
        for _orig, tmp_path in tmp_files:
            if tmp_path != _orig:  # Only delete files we created
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def _normalize_schema(raw: dict) -> dict:
    """Pass through static_analyzer.rb output. Ruby outputs lod, cmo directly.

    All keys (srp, lod, cmo, ocp, lsp, dip, isp, dry, information_expert,
    encapsulation, god_object, feature_envy, long_method, shotgun_surgery,
    parse_errors) are passed through unchanged.
    """
    return dict(raw)


def _remap_paths(findings: dict, path_map: dict[str, str]) -> dict:
    """Replace temp file paths in findings with original logical paths."""
    if not path_map:
        return findings

    raw = json.dumps(findings)
    for tmp_path, orig_path in path_map.items():
        # Use JSON-escaped versions so Windows backslashes (\\) are matched correctly
        json_tmp  = json.dumps(tmp_path)[1:-1]
        json_orig = json.dumps(orig_path)[1:-1]
        raw = raw.replace(json_tmp, json_orig)
    return json.loads(raw)
