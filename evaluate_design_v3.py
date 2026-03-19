#!/usr/bin/env python3
"""
evaluate_design_v3.py – Primary design violation evaluator (replaces evaluate_design_hybrid.py).

For each project:
  1. Load cached files from pr_cache/
  2. Run static analysis
  3. Build diff-anchored context (diff + touched symbol bodies, 20K cap)
  4. Retrieve 3 similar exemplars (excluding current project)
  5. Run 3 batch LLM calls (structural, method-level, coupling) + 1 alignment call
  6. Write to evaluations_v3-YYYY.jsonl

Optimizations: 3 short batch prompts; --fast skips alignment and exemplars; skips projects already in output.

Usage:
  python evaluate_design_v3.py --input dataset-2025.jsonl --output evaluations_v3-2025.jsonl \\
    --cache pr_cache/ --exemplars exemplar_index.json --model deepseek-coder-v2:16b-lite-instruct-q4_K_M \\
    --limit 10 --skip 0 --no-llm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd
from tqdm import tqdm

# Project imports
from build_exemplar_index import find_similar_exemplars
from static_analyzer.run_all import run_static_analysis

VIOLATION_TYPES = [
    "srp", "dry", "lod", "long_chain", "cmo", "lsp",
    "god_object", "feature_envy", "long_method", "shotgun_surgery", "ocp",
    "dip", "information_expert",
]

CONTEXT_CAP = 20_000
EXEMPLAR_QUERY_DIFF_CHARS = 500
EXEMPLAR_QUERY_FEEDBACK_CHARS = 200
ALIGNMENT_FEEDBACK_CHARS = 500

# Batch A — Structural: SRP, God Object, CMO, LSP, OCP
BATCH_A_TYPES = ["srp", "god_object", "cmo", "lsp", "ocp"]
BATCH_A_PROMPT = """Analyze this code for 5 violation types. Be concise. Only flag clear violations.
Focus ONLY on code added or modified in the diff (lines starting with +).
Do not flag violations in unchanged context lines.

{exemplar_context}

## Static analysis pre-scan
{static_summary}

{diff_anchored_context}

Return JSON only:
{{
  "srp": {{"violations": [{{"class": "X", "reason": "Y", "severity": 1-3}}], "count": N}},
  "god_object": {{"violations": [{{"class": "X", "reason": "Y", "severity": 1-3}}], "count": N}},
  "cmo": {{"violations": [{{"class": "X", "reason": "Y", "severity": 1-3}}], "count": N}},
  "lsp": {{"violations": [{{"class": "X", "method": "Y", "reason": "Z", "severity": 1-3}}], "count": N}},
  "ocp": {{"violations": [{{"class": "X", "method": "Y", "reason": "Z", "severity": 1-3}}], "count": N}}
}}

Definitions:
SRP: A class violates SRP if it has multiple unrelated responsibilities.
  Signal: class name requires "and" to describe (e.g. "handles auth AND sends email").

God Object: A class that accumulates too many methods (15+) or instance variables (10+),
  becoming a central hub that everything depends on.

CMO: Class where 50%+ of methods use self.method_name (Ruby) or are static (TS).
  Usually means procedural thinking disguised as OOP.

LSP: Subclass overrides parent method with different arity, raises new exceptions,
  or returns incompatible types. Breaks substitutability.

OCP: Uses long if/elsif/case or type-checking (is_a?, kind_of?) instead of
  polymorphism. Adding new types requires modifying existing code.

Report at most 3 violations per type. If you find more, report the 3 most severe.
Include the full total count (N) for each type even if you only list 3 violations."""

# Batch B — Method-level: Feature Envy, Long Method, DRY, Information Expert, DIP
BATCH_B_TYPES = ["feature_envy", "long_method", "dry", "information_expert", "dip"]
BATCH_B_PROMPT = """Analyze this code for 5 violation types. Be concise. Only flag clear violations.
For method-level violations, consider BOTH:
(a) methods ADDED in the diff (lines starting with +)
(b) methods in files touched by the diff that static analysis flagged
Use the static pre-scan above as your primary guide for what to investigate.

{exemplar_context}

## Static analysis pre-scan
{static_summary}

{diff_anchored_context}

Return JSON only:
{{
  "feature_envy": {{"violations": [{{"class": "X", "method": "Y", "reason": "Z", "severity": 1-3}}], "count": N}},
  "long_method": {{"violations": [{{"class": "X", "method": "Y", "line_count": N, "severity": 1-3}}], "count": N}},
  "dry": {{"violations": [{{"location": "X", "reason": "Y", "severity": 1-3}}], "count": N}},
  "information_expert": {{"violations": [{{"class": "X", "method": "Y", "reason": "Z", "severity": 1-3}}], "count": N}},
  "dip": {{"violations": [{{"class": "X", "instantiates": "Y", "reason": "Z", "severity": 1-3}}], "count": N}}
}}

Definitions:
Feature Envy: A method that calls methods on OTHER objects more than it uses its
  own data (@ivars). It "envies" another class and may belong there instead.

Long Method: Any method over 20 lines. Over 30 lines is almost always a violation.
  Look specifically at methods ADDED in the diff.

DRY: Identical or near-identical logic blocks that should be extracted into a
  shared method or module. Look for copy-pasted conditionals or loops.

Information Expert: A method that manipulates data primarily owned by another class.
  Different from Feature Envy: this is about data ownership, not method calls.
  Ask: does this method know too much about another class's internals?

DIP: A high-level class directly calls ClassName.new inside its methods, creating
  hard dependencies. Should use dependency injection or factories instead.

Report at most 3 violations per type. If you find more, report the 3 most severe.
Include the full total count (N) for each type even if you only list 3 violations."""

# Batch C — Coupling: LoD, Long Chain, Shotgun Surgery
BATCH_C_TYPES = ["lod", "long_chain", "shotgun_surgery"]
BATCH_C_PROMPT = """Analyze this code for 3 violation types. Be concise. Only flag clear violations.
Focus ONLY on code added or modified in the diff (lines starting with +).
Do not flag violations in unchanged context lines.

{exemplar_context}

## Static analysis pre-scan
{static_summary}

{diff_anchored_context}

Return JSON only:
{{
  "lod": {{"violations": [{{"location": "X", "chain": "Y", "reason": "Z", "severity": 1-3}}], "count": N}},
  "long_chain": {{"violations": [{{"location": "X", "chain": "Y", "severity": 1-3}}], "count": N}},
  "shotgun_surgery": {{"violations": [{{"concern": "X", "files_affected": ["Y"], "reason": "Z", "severity": 1-3}}], "count": N}}
}}

Definitions:
LoD: A method reaches through intermediate objects it doesn't own:
  a.b.c where b is not self, not a parameter, not an @ivar of this class.

Long Chain: Any chain of 5+ method calls regardless of ownership. Fragile code.

Shotgun Surgery: A single logical feature is scattered across many files,
  requiring changes in multiple places for one conceptual modification.

Report at most 3 violations per type. If you find more, report the 3 most severe.
Include the full total count (N) for each type even if you only list 3 violations."""

BATCH_PROMPTS: list[tuple[list[str], str]] = [
    (BATCH_A_TYPES, BATCH_A_PROMPT),
    (BATCH_B_TYPES, BATCH_B_PROMPT),
    (BATCH_C_TYPES, BATCH_C_PROMPT),
]


def _format_batch_b_violation(vt: str, v: dict) -> str:
    """Format a single violation for Batch B detailed summary."""
    cls = v.get("class_name", "?")
    method = v.get("method_name")
    if method:
        loc = f"{cls}#{method}"
    else:
        loc = cls
    if vt == "feature_envy":
        ext = v.get("external_references", 0)
        own = v.get("own_references", 0)
        return f"  - {loc} ({ext} external refs vs {own} own)"
    if vt == "long_method":
        lines = v.get("line_count", 0)
        return f"  - {loc} ({lines} lines)"
    if vt == "dry":
        dup = v.get("duplicate_count", 0)
        return f"  - {loc} (duplicates {dup} other(s))"
    if vt == "information_expert":
        ext = v.get("external_calls", 0)
        ivar = v.get("ivar_accesses", 0)
        return f"  - {loc} ({ext} external vs {ivar} ivar)"
    if vt == "dip":
        n = v.get("concretion_count", 0)
        return f"  - {cls} ({n} direct instantiations)"
    return f"  - {loc}"


def build_static_summary(static_findings: dict, types: list[str], detailed: bool = False) -> str:
    """
    Build static analysis pre-scan summary for a batch of violation types.
    If detailed=True (Batch B): show first 3 violations per type with method names.
    Otherwise: "Static pre-scan found: {summary}" or "Static pre-scan found no {type} violations."
    """
    summaries = static_findings.get("summaries", {})
    lines = []
    for vt in types:
        count = 0
        section = static_findings.get(vt, {})
        violations: list = []
        if isinstance(section, dict):
            c = section.get("count")
            violations = section.get("violations", section.get("signals", []))
            if c is None:
                c = len(violations) if violations else 0
            count = int(c) if c else 0
        summary = summaries.get(vt, "")
        if count > 0:
            if detailed and violations:
                top = violations[:3]
                inst_lines = [_format_batch_b_violation(vt, v) for v in top]
                lines.append(f"Static pre-scan found {count} {vt} violations. Top instances:\n" + "\n".join(inst_lines))
            elif summary:
                lines.append(f"Static pre-scan found: {summary}")
            else:
                lines.append(f"Static pre-scan found {count} {vt} violations.")
        else:
            lines.append(f"Static pre-scan found no {vt} violations.")
    return "\n".join(lines)


def _cache_filename_to_path(fname: str) -> str:
    """Convert 984e4438974d_app__controllers__x.rb -> app/controllers/x.rb"""
    if "_" not in fname:
        return fname
    parts = fname.split("_", 1)
    if len(parts) == 2 and len(parts[0]) >= 12:  # sha prefix
        return parts[1].replace("__", "/")
    return fname.replace("__", "/")


def load_semester_map(projects_csv: Path) -> dict[str, str]:
    """Load project_id -> semester from projects.csv (first column, forward-filled)."""
    if not projects_csv.exists():
        return {}
    df = pd.read_csv(projects_csv, encoding="utf-8")
    df.iloc[:, 0] = df.iloc[:, 0].replace("", pd.NA).ffill()
    first_col = df.columns[0]
    pid_col = "Project ID" if "Project ID" in df.columns else df.columns[1]
    return dict(zip(df[pid_col].astype(str).str.strip(), df[first_col].astype(str).str.strip()))


def load_cached_files(
    cache_dir: Path,
    project_id: str,
    allowed_paths: set[str] | None = None,
) -> list[dict[str, str]]:
    """
    Load .rb, .ts, .tsx files from pr_cache/<project_id>/files/.
    If allowed_paths is provided, only return files whose path is in that set.
    """
    files_dir = cache_dir / project_id / "files"
    if not files_dir.exists():
        return []
    result = []
    for p in files_dir.iterdir():
        if p.suffix.lower() in (".rb", ".ts", ".tsx"):
            try:
                path = _cache_filename_to_path(p.name)
                if allowed_paths is not None and path not in allowed_paths:
                    continue
                content = p.read_text(encoding="utf-8", errors="replace")
                result.append({"path": path, "content": content})
            except Exception:
                pass
    return result


def load_diff_from_cache(cache_dir: Path, project_id: str) -> str:
    """Load diff from pr_cache/<project_id>/diff.txt."""
    diff_path = cache_dir / project_id / "diff.txt"
    if diff_path.exists():
        return diff_path.read_text(encoding="utf-8", errors="replace")
    return ""


# Path prefixes to exclude from static analysis (migrations, tests)
STATIC_ANALYSIS_EXCLUDE_PREFIXES = ("db/migrate/", "spec/", "test/")


def detect_project_type(repo_name: str) -> tuple[str, str]:
    """
    Detect project_type and project_category from repo_name.
    Returns (project_type, project_category).
    """
    rn = (repo_name or "").lower().strip()
    if not rn:
        return ("unknown", "unknown")
    if "reimplementation-back-end" in rn or "reimplementation-backend" in rn or "reimplementation_backend" in rn:
        return ("reimplementation_backend", "reimplementation")
    if "reimplementation-frontend" in rn or "reimplementation_frontend" in rn or "front-end" in rn:
        return ("reimplementation_frontend", "reimplementation")
    if rn == "expertiza" or ("expertiza" in rn and "reimplementation" not in rn):
        return ("legacy", "refactoring")
    return ("unknown", "unknown")


def parse_diff_changed_files(diff: str) -> list[str]:
    """
    Parse diff to extract list of changed file paths.
    Uses lines starting with "diff --git a/X b/Y" to get file paths.
    Returns unique relative paths (normalized).
    """
    changed: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.search(r"diff --git a/(.+?) b/", line)
            if m:
                path = m.group(1).strip()
                if path and path != "/dev/null" and path not in changed:
                    changed.append(path)
    return changed


def parse_diff_touched_symbols(diff: str) -> dict[str, list[str]]:
    """
    Parse diff to extract file -> list of method/class/module names in + or - lines.
    Returns {filepath: [symbol_name, ...]}.
    """
    result: dict[str, list[str]] = {}
    current_file = None
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            m = re.search(r"diff --git a/(.+?) b/", line)
            if m:
                current_file = m.group(1).strip()
                if current_file not in result:
                    result[current_file] = []
        if current_file and (line.startswith("+") or line.startswith("-")):
            content = line[1:].strip()
            # Ruby: def method_name, class ClassName, module ModuleName
            for pat, name_grp in [
                (r"\bdef\s+([a-zA-Z_][a-zA-Z0-9_?!]*)", 1),
                (r"\bclass\s+([A-Z][a-zA-Z0-9_:]*)", 1),
                (r"\bmodule\s+([A-Z][a-zA-Z0-9_:]*)", 1),
            ]:
                for m in re.finditer(pat, content):
                    name = m.group(name_grp)
                    if name and name not in result[current_file]:
                        result[current_file].append(name)
    return result


def extract_method_body_ruby(source: str, method_name: str) -> str | None:
    """Extract Ruby method body from def method_name to matching end."""
    pat = rf"\bdef\s+(?:self\.)?{re.escape(method_name)}\b"
    m = re.search(pat, source)
    if not m:
        return None
    start = m.start()
    pos = m.end()
    depth = 1  # we're inside our def
    while pos < len(source):
        if pos + 3 <= len(source):
            tok = source[pos : pos + 3]
            if tok == "def" and (pos == 0 or source[pos - 1] in " \n\t"):
                depth += 1
            elif tok == "end" and (pos == 0 or source[pos - 1] in " \n\t"):
                depth -= 1
                if depth == 0:
                    return source[start : pos + 3]
        pos += 1
    return None


# File patterns to exclude from diff-anchored context (test/spec files)
CONTEXT_EXCLUDE_PATTERNS = (
    "spec/",
    "test/",
    "_spec.rb",
    "_test.rb",
    ".test.ts",
    ".test.tsx",
    ".spec.ts",
    ".spec.tsx",
)


def _is_test_or_spec_file(filepath: str) -> bool:
    """Return True if filepath should be excluded from LLM context."""
    fp = filepath.lower()
    return any(fp.startswith(p) or p in fp for p in CONTEXT_EXCLUDE_PATTERNS)


def _has_placeholder_violations(sub: dict) -> bool:
    """Return True if any violation has placeholder values (class X, location X, chain Y)."""
    viols = sub.get("violations", [])
    if not isinstance(viols, list):
        return False
    for v in viols:
        if not isinstance(v, dict):
            continue
        if v.get("class") == "X" or v.get("location") == "X" or v.get("chain") == "Y":
            return True
    return False


def _filter_diff_exclude_test_spec(diff: str) -> str:
    """Remove diff sections for test/spec files. Returns filtered diff."""
    if not diff.strip():
        return diff
    lines = diff.splitlines()
    out: list[str] = []
    skip_section = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.search(r"diff --git a/(.+?) b/", line)
            path = m.group(1).strip() if m else ""
            skip_section = _is_test_or_spec_file(path)
            if not skip_section:
                out.append(line)
        elif not skip_section:
            out.append(line)
        i += 1
    return "\n".join(out)


def build_diff_anchored_context(
    diff: str,
    file_contents: dict[str, str],
    touched: dict[str, list[str]],
    cap: int = CONTEXT_CAP,
) -> str:
    """
    Build context: diff + full method bodies for touched symbols.
    Excludes test/spec files from both diff and method bodies.
    Cap at cap chars, prioritizing diff then smallest bodies first.
    """
    filtered_diff = _filter_diff_exclude_test_spec(diff)
    parts = [f"## Diff\n{filtered_diff}\n\n## Full context for touched symbols\n"]
    used = len(parts[0])
    method_bodies: list[tuple[str, str, str]] = []  # (file, name, body)
    for filepath, symbols in touched.items():
        if _is_test_or_spec_file(filepath):
            continue
        content = file_contents.get(filepath)
        if not content:
            continue
        for sym in symbols:
            body = extract_method_body_ruby(content, sym)
            if body:
                method_bodies.append((filepath, sym, body))
    # Sort by body size ascending to fit more
    method_bodies.sort(key=lambda x: len(x[2]))
    for filepath, sym, body in method_bodies:
        block = f"\n### {filepath}\n```ruby\n{body}\n```\n"
        if used + len(block) <= cap:
            parts.append(block)
            used += len(block)
        else:
            break
    return "".join(parts)


EXEMPLAR_FEEDBACK_CHARS = 150
EXEMPLAR_MIN_SIMILARITY = 0.1


def format_exemplars(exemplars: list[dict], use_exemplars: bool = True) -> str:
    """Format exemplars for prompt. Max 1 exemplar, 150 chars feedback, skip if sim < 0.1."""
    if not use_exemplars or not exemplars:
        return ""
    ex = exemplars[0]
    if ex.get("similarity", 0) < EXEMPLAR_MIN_SIMILARITY:
        return ""
    pid = ex.get("project_id", "?")
    fb = (ex.get("project_feedback") or "")[:EXEMPLAR_FEEDBACK_CHARS]
    hints = ex.get("violation_hints", [])
    if hints:
        return f"## Reference example\nProject {pid} had these issues: {hints}. Feedback: {fb}\n\n"
    return f"## Reference example\nProject {pid}. Feedback: {fb}\n\n"


async def call_ollama(session: aiohttp.ClientSession, model: str, prompt: str, timeout: int = 120) -> dict | None:
    """Single Ollama generate call. Returns parsed JSON or None on failure."""
    url = "http://localhost:11434/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            text = data.get("response", "")
            return json.loads(text)
    except (asyncio.TimeoutError, json.JSONDecodeError, aiohttp.ClientError):
        return None


async def run_batch_llm_calls(
    session: aiohttp.ClientSession,
    model: str,
    exemplar_context: str,
    diff_anchored_context: str,
    static_findings: dict,
    project_id: str = "",
    timeout: int = 120,
) -> dict[str, dict]:
    """Run 3 batch prompts sequentially, merge into llm_findings."""
    results = {vt: {"violations": [], "count": 0} for vt in VIOLATION_TYPES}
    for types, batch_template in BATCH_PROMPTS:
        detailed = types == BATCH_B_TYPES  # Method-level: show concrete violation targets
        static_summary = build_static_summary(static_findings or {}, types, detailed=detailed)
        full_prompt = batch_template.format(
            exemplar_context=exemplar_context,
            static_summary=static_summary,
            diff_anchored_context=diff_anchored_context,
        )
        result = await call_ollama(session, model, full_prompt, timeout)
        if result and isinstance(result, dict):
            for vt in types:
                sub = result.get(vt)
                if sub and isinstance(sub, dict):
                    if _has_placeholder_violations(sub):
                        print(
                            f"[WARN] {project_id} batch returned placeholder values - likely empty context",
                            file=sys.stderr,
                        )
                        results[vt] = {"violations": [], "count": 0}
                    else:
                        results[vt] = sub
                else:
                    results[vt] = {"violations": [], "count": 0}
    return results


def compute_violation_counts(llm_findings: dict[str, dict]) -> dict[str, int]:
    """Extract counts from llm_findings."""
    counts = {}
    total = 0
    for vt in VIOLATION_TYPES:
        c = llm_findings.get(vt, {}).get("count", 0)
        if isinstance(c, list):
            c = len(c)
        counts[vt] = int(c) if c else 0
        total += counts[vt]
    counts["total"] = total
    return counts


def compute_static_counts(static_findings: dict) -> dict[str, int]:
    """Extract counts from static findings."""
    counts = {}
    total = 0
    for vt in VIOLATION_TYPES:
        section = static_findings.get(vt, {})
        c = section.get("count")
        if c is None:
            items = section.get("violations", section.get("signals", []))
            c = len(items) if items else 0
        counts[vt] = int(c) if c else 0
        total += counts[vt]
    counts["total"] = total
    return counts


def format_violation_summary(counts: dict[str, int]) -> str:
    """Format violation counts for alignment prompt."""
    lines = []
    for vt in VIOLATION_TYPES:
        lines.append(f"- {vt}: {counts.get(vt, 0)}")
    return "\n".join(lines)


async def call_alignment(
    session: aiohttp.ClientSession,
    model: str,
    project_feedback: str,
    violation_summary: str,
    timeout: int = 60,
) -> tuple[int, str, bool]:
    """Call LLM for alignment score. Returns (score, explanation, feedback_mentions_violations)."""
    prompt = f"""You are evaluating whether automated code analysis is CONSISTENT WITH instructor feedback.
Important: Instructor feedback is a holistic project grade, not a violation audit.
A project can receive positive feedback AND still have code violations — these are not contradictory.

Instructor feedback: {project_feedback[:ALIGNMENT_FEEDBACK_CHARS]}

Automated analysis found these violations:
{violation_summary}

Scoring guide:
5 = Feedback explicitly mentions specific issues that match what analysis found
    (e.g. feedback says 'too many responsibilities' and SRP violations were found)
4 = Feedback mentions general code quality issues consistent with findings
    (e.g. feedback says 'needs refactoring' and multiple violations found)
3 = Feedback is neutral or purely positive — cannot confirm or deny findings
    (this is the DEFAULT for positive feedback that doesn't mention code quality)
2 = Feedback praises specific design aspects that analysis flagged as violated
    (e.g. feedback says 'excellent use of SRP' but analysis found SRP violations)
1 = Feedback explicitly contradicts the analysis in a specific way

Note: positive feedback alone should default to score 3, NOT score 1 or 2.

Return ONLY: {{"alignment_score": N, "explanation": "...", "feedback_mentions_violations": true/false}}"""
    result = await call_ollama(session, model, prompt, timeout)
    if result:
        score = result.get("alignment_score", 3)
        expl = result.get("explanation", "")
        mentions = result.get("feedback_mentions_violations", False)
        return (
            int(score) if isinstance(score, (int, float)) else 3,
            str(expl),
            bool(mentions),
        )
    return (3, "Could not compute alignment.", False)


def process_project(
    project_id: str,
    record: dict,
    cache_dir: Path,
    exemplar_index: dict | list,
    model: str,
    semester_map: dict[str, str],
    no_llm: bool,
    fast: bool = False,
) -> dict[str, Any]:
    """Process one project through all stages."""
    project_feedback = (record.get("project_feedback") or "")[:500]
    if not project_feedback.strip():
        print(f"[WARN] Empty project_feedback for project_id={project_id}", file=sys.stderr)
    diff = load_diff_from_cache(cache_dir, project_id) or record.get("diff", "")
    full_files = record.get("full_files", [])

    # Parse changed files from diff (before loading) for scoped static analysis
    changed_files = parse_diff_changed_files(diff)
    files_in_pr = len(changed_files)
    analyze_paths = {
        p for p in changed_files
        if not any(p.startswith(prefix) for prefix in STATIC_ANALYSIS_EXCLUDE_PREFIXES)
    }

    # Stage 1: Load cached files (only those touched by diff)
    changed_paths = set(changed_files) if changed_files else None
    files = load_cached_files(cache_dir, project_id, allowed_paths=changed_paths)
    if not files and full_files:
        files = [
            {"path": f.get("path", ""), "content": f.get("content", "")}
            for f in full_files
            if f.get("content") and (not changed_paths or f.get("path", "") in changed_paths)
        ]

    file_contents = {f["path"]: f["content"] for f in files if f.get("path") and f.get("content")}

    # Stage 2: Static analysis (only changed files, excluding migrate/spec/test)
    files_for_static = [f for f in files if f.get("path") in analyze_paths]
    static_findings = run_static_analysis(files_for_static) if files_for_static else {}
    static_counts = compute_static_counts(static_findings)
    files_statically_analyzed = len(files_for_static)

    # Stage 3: Diff-anchored context
    touched = parse_diff_touched_symbols(diff)
    diff_anchored = build_diff_anchored_context(diff, file_contents, touched)

    # Stage 4: Exemplar retrieval (skipped when --fast)
    exemplar_ids: list[str] = []
    if fast:
        exemplar_context = ""
    else:
        query = (diff[:EXEMPLAR_QUERY_DIFF_CHARS] or "") + " " + (project_feedback[:EXEMPLAR_QUERY_FEEDBACK_CHARS] or "")
        exemplars = find_similar_exemplars(query, exemplar_index, top_k=1, exclude_id=project_id)
        exemplar_context = format_exemplars(exemplars, use_exemplars=True)
        exemplar_ids = [e.get("project_id", "") for e in exemplars]

    # Stage 5 & 6: LLM calls
    llm_findings = {}
    alignment_score = 3
    alignment_explanation = ""
    feedback_mentions_violations = False
    llm_failed = False

    if not no_llm:
        try:
            async def _run_batches():
                async with aiohttp.ClientSession() as sess:
                    return await run_batch_llm_calls(
                        sess, model, exemplar_context, diff_anchored, static_findings,
                        project_id=project_id,
                    )
            llm_findings = asyncio.run(_run_batches())
        except Exception:
            llm_findings = {vt: {"violations": [], "count": 0} for vt in VIOLATION_TYPES}
            llm_failed = True

        violation_counts = compute_violation_counts(llm_findings)
        if not fast:
            summary = format_violation_summary(violation_counts)
            try:
                async def _align():
                    async with aiohttp.ClientSession() as sess:
                        return await call_alignment(sess, model, project_feedback, summary)
                alignment_score, alignment_explanation, feedback_mentions_violations = asyncio.run(_align())
            except Exception:
                alignment_explanation = "Alignment call failed."
    else:
        llm_findings = {vt: {"violations": [], "count": 0} for vt in VIOLATION_TYPES}
        violation_counts = compute_violation_counts(llm_findings)

    semester = semester_map.get(project_id, "")
    repo_name = record.get("repo_name", "") or ""
    project_type, project_category = detect_project_type(repo_name)

    return {
        "project_id": project_id,
        "semester": semester,
        "repo_name": repo_name,
        "project_type": project_type,
        "project_category": project_category,
        "files_in_pr": files_in_pr,
        "files_statically_analyzed": files_statically_analyzed,
        "static_findings": static_findings,
        "llm_findings": llm_findings,
        "violation_counts": compute_violation_counts(llm_findings),
        "static_counts": static_counts,
        "alignment_score": alignment_score,
        "alignment_explanation": alignment_explanation,
        "feedback_mentions_violations": feedback_mentions_violations,
        "exemplars_used": exemplar_ids,
        "llm_failed": llm_failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate design violations (v3)")
    parser.add_argument("--input", "-i", type=Path, required=True, help="Input JSONL (dataset-YYYY.jsonl)")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Output JSONL (evaluations_v3-YYYY.jsonl)")
    parser.add_argument("--cache", "-c", type=Path, default=Path("pr_cache"), help="pr_cache directory")
    parser.add_argument("--exemplars", "-e", type=Path, default=Path("exemplar_index.json"), help="Exemplar index JSON")
    parser.add_argument("--projects", "-p", type=Path, default=Path("projects.csv"), help="projects.csv for semester")
    parser.add_argument("--model", "-m", type=str, default="deepseek-coder-v2:16b-lite-instruct-q4_K_M")
    parser.add_argument("--limit", "-n", type=int, default=None)
    parser.add_argument("--skip", "-s", type=int, default=0)
    parser.add_argument("--concurrency", "-j", type=int, default=3, help="Max parallel projects")
    parser.add_argument("--fast", action="store_true", help="Skip alignment and exemplars for bulk runs")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM, static analysis only")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: Input not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    exemplar_index = {}
    if args.exemplars.exists():
        exemplar_index = json.loads(args.exemplars.read_text(encoding="utf-8"))

    semester_map = load_semester_map(args.projects)

    records = []
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if args.skip:
        records = records[args.skip:]
    if args.limit:
        records = records[: args.limit]

    # Optimization 4: Skip projects already in output file
    already_done: set[str] = set()
    if args.output.exists():
        with open(args.output, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                        pid = r.get("project_id")
                        if pid:
                            already_done.add(pid)
                    except json.JSONDecodeError:
                        pass
    if already_done:
        records = [r for r in records if r.get("project_id") not in already_done]
        print(f"Skipping {len(already_done)} already processed. {len(records)} remaining.")

    if not records:
        print("No records to process.")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    total_violations: dict[str, int] = {vt: 0 for vt in VIOLATION_TYPES}
    total_violations["total"] = 0
    alignment_scores: list[int] = []
    mode = "a" if already_done else "w"
    write_lock = threading.Lock()
    concurrency = max(1, args.concurrency)

    def process_one(rec: dict) -> tuple[str, dict | None, Exception | None]:
        """Process one record; returns (project_id, result, error)."""
        pid = rec.get("project_id", "")
        if not pid:
            return ("", None, None)
        try:
            result = process_project(
                pid, rec, args.cache, exemplar_index, args.model, semester_map, args.no_llm, args.fast
            )
            return (pid, result, None)
        except Exception as e:
            return (pid, None, e)

    start_time = time.perf_counter()
    with open(args.output, mode, encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(process_one, rec): rec for rec in records if rec.get("project_id")}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Evaluating"):
                pid, result, err = fut.result()
                if err:
                    print(f"\nError processing {pid}: {err}", file=sys.stderr)
                    continue
                if not result:
                    continue
                with write_lock:
                    for vt in VIOLATION_TYPES:
                        total_violations[vt] += result.get("violation_counts", {}).get(vt, 0)
                    total_violations["total"] += result.get("violation_counts", {}).get("total", 0)
                    alignment_scores.append(result.get("alignment_score", 3))
                    out.write(json.dumps(result, ensure_ascii=False) + "\n")
                    out.flush()

    elapsed = time.perf_counter() - start_time
    n_done = len(alignment_scores)
    print("\n" + "=" * 50)
    print("Summary")
    print("=" * 50)
    print(f"Projects processed: {n_done}")
    print(f"Total time: {elapsed:.1f}s ({elapsed / n_done:.1f}s per project)" if n_done else f"Total time: {elapsed:.1f}s")
    print("Violations per type:")
    for vt in VIOLATION_TYPES:
        print(f"  {vt}: {total_violations.get(vt, 0)}")
    print(f"  total: {total_violations.get('total', 0)}")
    if alignment_scores:
        print(f"Avg alignment score: {sum(alignment_scores) / len(alignment_scores):.2f}")


if __name__ == "__main__":
    main()
