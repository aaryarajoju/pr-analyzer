#!/usr/bin/env python3
"""
generate_teaching_insights.py

Select instructive examples from evaluation JSONL, attach diff/code context,
and call Ollama (3 calls per violation type: recent, older, synthesize) to produce
pedagogical insights for thesis / teaching.

Usage:
  python generate_teaching_insights.py \\
    --input "evaluations_v4-*.jsonl" \\
    --cache pr_cache/ \\
    --output teaching_insights.json \\
    --model deepseek-coder-v2:16b-lite-instruct-q4_K_M

  # Single violation type only:
  python generate_teaching_insights.py ... --violation feature_envy
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

VIOLATION_TYPES = ["feature_envy", "information_expert", "lod", "shotgun_surgery"]

DISPLAY_NAMES = {
    "feature_envy": "Feature Envy",
    "information_expert": "Information Expert",
    "lod": "Law of Demeter (LoD)",
    "shotgun_surgery": "Shotgun Surgery",
}

# At least one substring (case-insensitive) must appear in pattern_summary + student_misconception
# feature_envy negative check: see FEATURE_ENVY_OFF_TOPIC_NEGATIVE
ON_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "feature_envy": [
        "envy",
        "external",
        "other class",
        "belongs",
        "method uses",
        "data from",
        "more interested",
        "responsibility",
        "envies",
    ],
    "information_expert": [
        "information",
        "expert",
        "data",
        "responsibility",
        "belongs",
        "owns",
    ],
    "lod": [
        "demeter",
        "chain",
        "stranger",
        "intermediary",
        "access",
        "dot",
    ],
    "shotgun_surgery": [
        "scattered",
        "spread",
        "multiple files",
        "shotgun",
        "concern",
        "change",
        "files",
        "propagat",
        "coupled",
        "cohes",
        "feature",
    ],
}

# Shotgun Surgery: no single-file diff/code in prompts; payload still carries N/A for JSON export
SHOTGUN_CROSS_FILE_NA = "N/A - Shotgun Surgery is a cross-file concern"

# Feature Envy: React/TS signals in violation_detail (reason / class / method)
FEATURE_ENVY_REACT_DETAIL_KEYWORDS = [
    "react",
    "component",
    "hook",
    "usestate",
    "useeffect",
    "props",
    "tsx",
    "jsx",
    "dom",
    "lifecycle",
]

# If pattern_summary or student_misconception matches any of these, treat as off-topic (feature_envy only)
FEATURE_ENVY_OFF_TOPIC_NEGATIVE = [
    "react",
    "lifecycle",
    "dom",
    "state management",
    "usestate",
    "useeffect",
    "hooks",
    "component",
    "frontend framework",
]


def violation_type_reinforcement(display_name: str) -> str:
    """Opening block for all teaching-insight prompts: single-violation focus + definitions."""
    return f"""IMPORTANT: You are analyzing ONLY the following violation type:
{display_name}

Definition:
- Feature Envy: A method that uses data/methods from OTHER classes more
  than its own class. The method seems more interested in another class.
A method that references another object's methods or attributes MORE
than its own class's data (@ivars in Ruby, this.* in TS).
Key test: compare external_references (other objects) to own_references (self/this).
If external references > own references, the method likely belongs in the other class.
Exclude infrastructure calls (logger, Rails helpers, framework DSL) from the count.
Example: a method in OrderController that reads item.price, item.quantity, item.discount
to compute a total - that logic belongs in Item or a calculator, not the controller.


- Information Expert: A method that operates on data primarily owned by
  another class — it would be better placed in the class that owns that data.
A method that reads or manipulates data primarily OWNED by
another class to produce a result that the other class should compute itself.
Different from Feature Envy: Feature Envy is about where a method lives
(it should move). Information Expert is about who should own the computation
(the class that owns the data should expose the result).
Example: OrderPrinter#total_weight that sums item.weight for each item -
Order or Item should expose total_weight, not the printer.
Key test: does this method need to know the internal structure of another
class's data to do its job? If yes, the other class should provide a method instead.

- Law of Demeter: A method that accesses an object through a foreign
  intermediary it does not own (e.g. a.b.c where b is not self or a
  direct component). Also called 'talking to strangers'.
 A method should only call methods on: (1) self,
  (2) parameters, (3) objects it creates, (4) its own @ivars.
  Violation: reaching through objects (e.g., user.account.subscription.plan).
  Fix: delegate methods (e.g., user.subscription_plan).


- Shotgun Surgery: When ONE logical feature or concern is scattered across
MANY files and classes simultaneously. A single logical concern is scattered across
many files/classes, meaning one conceptual change requires editing many
places at once. Typically 4+ files; signals include similar modifications in
multiple classes (e.g. adding a field touches model, controller, serializer, view),
or the same constant/string added in many places — indicates a missing abstraction layer.
The symptom is that a single conceptual change (e.g. adding a new user permission type)
requires editing 5+ different files. This is NOT about class size, method count, or
authorization. It is purely about scattered change impact across the codebase.
Example: adding a new assignment type requires changes in assignment.rb,
assignments_controller.rb, assignment_helper.rb, assignment_form.rb,
and the corresponding spec files. That is Shotgun Surgery.

Do NOT discuss security, authorization, database patterns, or any other
violation type. Focus ONLY on {display_name}.

"""

RECENT_YEARS = [2023, 2024, 2025]
OLDER_YEARS = list(range(2016, 2023))

MAX_DIFF_CHARS = 12_000
MAX_CONTEXT_CHARS = 8_000


def normalize_semester_year(semester: str) -> Optional[int]:
    """Extract 4-digit year from semester string."""
    if not semester or not isinstance(semester, str):
        return None
    s = re.sub(r"\s*\([^)]*\)\s*", " ", semester).strip()
    m = re.search(r"(\d{4})", s)
    if m:
        return int(m.group(1))
    return None


def display_category(project_category: str) -> str:
    """Map evaluation project_category to legacy | reimplementation | other."""
    pc = (project_category or "").strip().lower()
    if pc == "refactoring":
        return "legacy"
    if pc == "reimplementation":
        return "reimplementation"
    return "other"


def is_reimplementation_frontend_project(candidate: dict) -> bool:
    """True if project is the front-end reimplementation (JSON uses project_type)."""
    pt = (candidate.get("project_type") or "").strip().lower()
    if pt == "reimplementation_frontend":
        return True
    pc = (candidate.get("project_category") or "").strip().lower()
    return pc == "reimplementation_frontend"


def feature_envy_violation_detail_text(viol: dict) -> str:
    """Concatenate fields used to detect React/TS context in a violation."""
    if not isinstance(viol, dict):
        return ""
    parts = [
        str(viol.get("reason") or ""),
        str(viol.get("class") or viol.get("class_name") or ""),
        str(viol.get("method") or viol.get("method_name") or ""),
    ]
    return " ".join(parts).lower()


def violation_detail_has_react_ts_keywords(viol: dict) -> bool:
    """True if reason/class/method text suggests React/TypeScript (deprioritize for Feature Envy)."""
    t = feature_envy_violation_detail_text(viol)
    if not t:
        return False
    return any(kw in t for kw in FEATURE_ENVY_REACT_DETAIL_KEYWORDS)


def severity_value(v: dict) -> int:
    s = v.get("severity")
    try:
        i = int(s) if s is not None else 1
        return max(1, min(3, i))
    except (TypeError, ValueError):
        return 1


def cache_filename_to_path(fname: str) -> str:
    if "_" not in fname:
        return fname
    parts = fname.split("_", 1)
    if len(parts) == 2 and len(parts[0]) >= 12:
        return parts[1].replace("__", "/")
    return fname.replace("__", "/")


def find_file_in_cache(cache_dir: Path, project_id: str, logical_path: str) -> Optional[Path]:
    """Resolve logical repo path to a file under pr_cache/<id>/files/."""
    files_dir = cache_dir / project_id / "files"
    if not files_dir.is_dir():
        return None
    logical_norm = logical_path.replace("\\", "/").lstrip("/")
    tail = logical_norm.split("/")[-1] if "/" in logical_norm else logical_norm
    for p in files_dir.iterdir():
        if not p.is_file():
            continue
        lp = cache_filename_to_path(p.name)
        if lp == logical_norm or lp.endswith("/" + tail) or lp.endswith(logical_norm):
            return p
    return None


def guess_file_from_static(record: dict, vt: str, viol: dict) -> Optional[str]:
    """Try to find a file path in static_findings for this violation (class/method match)."""
    cls = viol.get("class") or viol.get("class_name")
    meth = viol.get("method")
    sf = record.get("static_findings") or {}
    section = sf.get(vt) or {}
    items = section.get("violations", section.get("signals", []))
    if not isinstance(items, list):
        return None
    for it in items:
        if not isinstance(it, dict):
            continue
        if cls and it.get("class_name") == cls:
            if meth and it.get("method_name") == meth:
                return it.get("file")
            if not meth:
                return it.get("file")
        if cls and str(it.get("class_name", "")).endswith(str(cls).split("::")[-1]):
            return it.get("file")
    for it in items:
        if isinstance(it, dict) and it.get("file"):
            return it.get("file")
    return None


def guess_file_from_violation(viol: dict) -> Optional[str]:
    loc = viol.get("location") or ""
    if "/" in loc or loc.endswith(".rb") or loc.endswith(".tsx") or loc.endswith(".ts"):
        # e.g. "app/models/foo.rb" or "path#method"
        if "#" in loc:
            loc = loc.split("#")[0]
        if "/" in loc:
            return loc.strip()
    return viol.get("file")


def extract_diff_section(diff_text: str, file_hint: Optional[str]) -> str:
    """Return the diff chunk for a file if found, else truncated full diff."""
    if not diff_text.strip():
        return ""
    if not file_hint:
        return diff_text[:MAX_DIFF_CHARS]
    hint = file_hint.replace("\\", "/")
    tail = hint.split("/")[-1]
    parts = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
    for part in parts:
        if not part.strip():
            continue
        if hint in part or tail in part:
            return part[:MAX_DIFF_CHARS]
    if hint in diff_text:
        idx = diff_text.index(hint)
        start = max(0, idx - 200)
        return diff_text[start : start + MAX_DIFF_CHARS]
    return diff_text[:MAX_DIFF_CHARS]


def read_codebase_context(cache_dir: Path, project_id: str, file_path: Optional[str]) -> str:
    if not file_path:
        return ""
    p = find_file_in_cache(cache_dir, project_id, file_path)
    if not p or not p.exists():
        logger.warning("Missing cache file for %s: %s", project_id, file_path)
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:MAX_CONTEXT_CHARS]
    except OSError as e:
        logger.warning("Could not read %s: %s", p, e)
        return ""


def flatten_violation_candidates(records: list[dict], vt: str) -> list[dict]:
    """One entry per (record, single violation dict) with metadata."""
    out: list[dict] = []
    for r in records:
        lf = r.get("llm_findings") or {}
        section = lf.get(vt) or {}
        viols = section.get("violations") or []
        cnt = section.get("count") or 0
        if not viols and (not cnt or int(cnt) <= 0):
            continue
        if not viols:
            continue
        semester = r.get("semester") or ""
        year = normalize_semester_year(semester)
        if year is None:
            continue
        pc = display_category(r.get("project_category") or "")
        pid = r.get("project_id") or ""
        project_type = r.get("project_type") or ""
        for v in viols:
            if not isinstance(v, dict):
                continue
            out.append({
                "project_id": pid,
                "semester": semester,
                "year": year,
                "project_category": pc,
                "project_type": project_type,
                "record": r,
                "violation_detail": v,
                "severity": severity_value(v),
            })
    out.sort(key=lambda x: -x["severity"])
    return out


def pick_with_variety(candidates: list[dict], limit: int) -> list[dict]:
    """Take up to `limit` items, preferring at least one legacy and one reimplementation when possible."""
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda x: -x["severity"])
    legacy = [c for c in candidates if c["project_category"] == "legacy"]
    reimpl = [c for c in candidates if c["project_category"] == "reimplementation"]
    picked: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def k(c: dict) -> tuple[str, str]:
        return (c["project_id"], json.dumps(c["violation_detail"], sort_keys=True))

    def add(c: dict) -> bool:
        if len(picked) >= limit:
            return False
        key = k(c)
        if key in seen:
            return False
        picked.append(c)
        seen.add(key)
        return True

    if legacy:
        add(legacy[0])
    if reimpl:
        add(reimpl[0])
    for c in candidates:
        if len(picked) >= limit:
            break
        add(c)
    return picked[:limit]


def _feature_envy_sort_key(c: dict) -> tuple[int, int]:
    """Lower tuple = higher priority: legacy non-React > reimpl non-React > other > React-detail."""
    v = c.get("violation_detail") or {}
    react = violation_detail_has_react_ts_keywords(v)
    pc = c.get("project_category") or ""
    if react:
        tier = 3
    elif pc == "legacy":
        tier = 0
    elif pc == "reimplementation":
        tier = 1
    else:
        tier = 2
    return (tier, -c.get("severity", 1))


def pick_feature_envy_for_year(year_cands: list[dict], limit: int) -> list[dict]:
    """
    Prefer legacy (Ruby) examples; deprioritize React-detail strings and reimplementation_frontend.
    Include reimplementation_frontend only if fewer than 3 legacy examples exist for this year.
    """
    if not year_cands:
        return []
    ruby_count = sum(1 for c in year_cands if c.get("project_category") == "legacy")
    allow_frontend = ruby_count < 3

    front = [c for c in year_cands if is_reimplementation_frontend_project(c)]
    non_front = [c for c in year_cands if not is_reimplementation_frontend_project(c)]

    if allow_frontend:
        pool = sorted(non_front, key=_feature_envy_sort_key) + sorted(front, key=_feature_envy_sort_key)
    else:
        pool = sorted(non_front, key=_feature_envy_sort_key)

    picked: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def dedup_key(c: dict) -> tuple[str, str]:
        return (c["project_id"], json.dumps(c["violation_detail"], sort_keys=True))

    for c in pool:
        if len(picked) >= limit:
            break
        dk = dedup_key(c)
        if dk in seen:
            continue
        seen.add(dk)
        out = dict(c)
        if is_reimplementation_frontend_project(c):
            out["is_react_context"] = True
        picked.append(out)
    return picked[:limit]


def select_examples_for_violation_type(
    candidates: list[dict],
    years: list[int],
    per_year_limit: int,
    violation_type: str = "",
) -> list[dict]:
    """Select examples: per_year_limit per year in `years`, with variety."""
    selected: list[dict] = []
    for y in years:
        year_cands = [c for c in candidates if c["year"] == y]
        if not year_cands:
            continue
        if violation_type == "feature_envy":
            picked = pick_feature_envy_for_year(year_cands, per_year_limit)
        else:
            year_cands.sort(key=lambda x: -x["severity"])
            picked = pick_with_variety(year_cands, per_year_limit)
        for p in picked:
            period = "recent" if y in RECENT_YEARS else "older"
            p2 = {**p, "period": period}
            selected.append(p2)
    return selected


def build_example_payload(
    cache_dir: Path,
    ex: dict,
    vt: str,
) -> dict:
    r = ex["record"]
    pid = ex["project_id"]
    viol = ex["violation_detail"]

    if vt == "shotgun_surgery":
        return {
            "project_id": pid,
            "semester": ex["semester"],
            "project_category": ex["project_category"],
            "period": ex["period"],
            "violation_type": vt,
            "violation_detail": viol,
            "diff_hunk": SHOTGUN_CROSS_FILE_NA,
            "codebase_context": SHOTGUN_CROSS_FILE_NA,
        }

    diff_path = cache_dir / pid / "diff.txt"
    diff_text = ""
    if diff_path.exists():
        try:
            diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("Could not read diff for %s: %s", pid, e)
    else:
        logger.warning("Missing diff: %s", diff_path)

    file_hint = guess_file_from_static(r, vt, viol) or guess_file_from_violation(viol)
    diff_hunk = extract_diff_section(diff_text, file_hint)
    codebase_context = read_codebase_context(cache_dir, pid, file_hint)

    payload: dict[str, Any] = {
        "project_id": pid,
        "semester": ex["semester"],
        "project_category": ex["project_category"],
        "period": ex["period"],
        "violation_type": vt,
        "violation_detail": viol,
        "diff_hunk": diff_hunk,
        "codebase_context": codebase_context or "(context unavailable)",
    }
    if ex.get("is_react_context"):
        payload["is_react_context"] = True
    return payload


def compute_stats(records: list[dict], vt: str) -> tuple[int, float, float, float]:
    """total_occurrences sum, pct affected, legacy avg, reimplementation avg (mean count per project in cohort, incl. zeros)."""
    total_occ = 0
    affected = 0
    legacy_counts: list[int] = []
    reimpl_counts: list[int] = []
    n = len(records)
    for r in records:
        vc = r.get("violation_counts") or {}
        c = int(vc.get(vt, 0) or 0)
        total_occ += c
        if c > 0:
            affected += 1
        pc = (r.get("project_category") or "").lower()
        if pc == "refactoring":
            legacy_counts.append(c)
        elif pc == "reimplementation":
            reimpl_counts.append(c)
    pct = 100.0 * affected / n if n else 0.0
    leg_avg = sum(legacy_counts) / len(legacy_counts) if legacy_counts else 0.0
    re_avg = sum(reimpl_counts) / len(reimpl_counts) if reimpl_counts else 0.0
    return total_occ, pct, leg_avg, re_avg


async def call_ollama_json(
    session: aiohttp.ClientSession,
    model: str,
    prompt: str,
    timeout: int = 240,
) -> dict[str, Any]:
    url = "http://localhost:11434/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                logger.error("Ollama HTTP %s", resp.status)
                return {}
            data = await resp.json()
            text = data.get("response", "")
            return json.loads(text)
    except (asyncio.TimeoutError, json.JSONDecodeError, aiohttp.ClientError) as e:
        logger.error("Ollama call failed: %s", e)
        return {}


def response_is_on_topic(vt: str, resp: dict[str, Any]) -> bool:
    """Check pattern_summary + student_misconception for violation-type keywords."""
    ps = (resp.get("pattern_summary") or "") + " " + (resp.get("student_misconception") or "")
    if not str(ps).strip():
        return False
    t = ps.lower()
    if vt == "feature_envy":
        for neg in FEATURE_ENVY_OFF_TOPIC_NEGATIVE:
            n = neg.lower()
            # Avoid substring false positives (e.g. "reaction", "domain")
            if n == "react":
                if re.search(r"\breact\b", t):
                    return False
            elif n == "dom":
                if re.search(r"\bdom\b", t):
                    return False
            elif n in t:
                return False
    for kw in ON_TOPIC_KEYWORDS.get(vt, []):
        if kw.lower() in t:
            return True
    return False


async def call_ollama_json_validated(
    session: aiohttp.ClientSession,
    model: str,
    vt: str,
    prompt: str,
    call_num: int,
    timeout: int = 300,
) -> tuple[dict[str, Any], bool]:
    """
    Call Ollama; validate on-topic keywords in pattern_summary + student_misconception.
    Retry once on failure. Returns (parsed_json, off_topic_warning) where
    off_topic_warning is True if both attempts fail validation.
    """
    r = await call_ollama_json(session, model, prompt, timeout=timeout)
    if response_is_on_topic(vt, r):
        return r, False
    logger.warning(
        "[WARN] %s Call %s response appears off-topic, retrying once",
        vt,
        call_num,
    )
    r2 = await call_ollama_json(session, model, prompt, timeout=timeout)
    if response_is_on_topic(vt, r2):
        return r2, False
    return (r2 if r2 else r), True


def build_examples_prompt_block(examples: list[dict]) -> str:
    parts = []
    for i, ex in enumerate(examples, start=1):
        header = (
            f"--- Example {i} ({ex.get('period')}, {ex.get('project_category')} project, {ex.get('semester')}) ---\n"
        )
        if ex.get("violation_type") == "shotgun_surgery":
            viol = ex.get("violation_detail") or {}
            parts.append(
                header
                + f"Concern: {viol.get('concern', 'unknown')}\n"
                f"Files that would need to change: {viol.get('files_affected', [])}\n"
                f"Reason: {viol.get('reason', '')}\n"
                f"Severity: {viol.get('severity', '')}\n"
            )
            continue

        vd = json.dumps(ex.get("violation_detail"), ensure_ascii=False)
        react_note = ""
        if ex.get("is_react_context"):
            react_note = (
                "[Note: This is a TypeScript/React context — focus on method responsibility, "
                "not React-specific patterns]\n"
            )
        parts.append(
            header
            + f"Violation: {vd}\n"
            + react_note
            + f"What the student changed (diff):\n{ex.get('diff_hunk', '')}\n"
            f"Surrounding codebase context:\n{ex.get('codebase_context', '')}\n"
        )
    return "\n".join(parts)


def insight_prompt_partial(
    display_name: str,
    pct: float,
    legacy_avg: float,
    reimpl_avg: float,
    examples: list[dict],
) -> str:
    block = build_examples_prompt_block(examples)
    n = len(examples)
    header = violation_type_reinforcement(display_name)
    return header + f"""You are an experienced software engineering educator analyzing
recurring design mistakes in a graduate OSS software engineering course.
Students submit Pull Requests to a real open-source Ruby on Rails project.
Some students work on legacy refactoring projects (modifying existing code),
others work on reimplementation projects (writing new code from scratch).

Violation type: {display_name}
Affected {pct:.1f}% of all student projects.
Average per legacy project: {legacy_avg:.2f}
Average per reimplementation project: {reimpl_avg:.2f}

Here are {n} real examples from student submissions, with the code they
changed and the surrounding codebase context:

{block}

Based on these real examples, answer:
1. What is the consistent pattern in how students introduce this violation?
   Note any differences between legacy and reimplementation projects.
2. What fundamental misconception about object-oriented design leads
   to this mistake?
3. What specific, concrete teaching intervention would best address this?
   (e.g. a specific exercise, lecture framing, code review checklist item,
   or assignment constraint)

Return ONLY valid JSON (no markdown):
{{
  "pattern_summary": "2-3 sentences on the consistent pattern, noting legacy vs reimplementation differences if any",
  "student_misconception": "1-2 sentences on the root OOP misconception",
  "teaching_suggestion": "2-3 sentences with a concrete actionable intervention",
  "legacy_vs_reimplementation_note": "1 sentence on whether violation manifests differently by project type"
}}
"""


def build_synthesize_prompt_body(
    display_name: str,
    recent_json: dict[str, Any],
    older_json: dict[str, Any],
) -> str:
    header = violation_type_reinforcement(display_name)
    return header + f"""You are an experienced software engineering educator. Merge two partial analyses
of the same violation type ({display_name}) from different time periods (recent 2023-2025 vs older 2016-2022).

Partial analysis — recent period:
{json.dumps(recent_json, ensure_ascii=False, indent=2)}

Partial analysis — older period:
{json.dumps(older_json, ensure_ascii=False, indent=2)}

Return ONLY valid JSON (no markdown) with unified fields:
{{
  "pattern_summary": "2-3 sentences synthesizing both periods",
  "student_misconception": "1-2 sentences",
  "teaching_suggestion": "2-3 sentences",
  "legacy_vs_reimplementation_note": "1 sentence"
}}
"""


async def run_insight_for_type(
    session: aiohttp.ClientSession,
    model: str,
    vt: str,
    records: list[dict],
    cache_dir: Path,
) -> dict[str, Any]:
    display_name = DISPLAY_NAMES[vt]
    total_occ, pct, leg_avg, re_avg = compute_stats(records, vt)
    candidates = flatten_violation_candidates(records, vt)

    recent_sel = select_examples_for_violation_type(candidates, RECENT_YEARS, 4, vt)
    older_sel = select_examples_for_violation_type(candidates, OLDER_YEARS, 2, vt)

    recent_payloads = [build_example_payload(cache_dir, ex, vt) for ex in recent_sel]
    older_payloads = [build_example_payload(cache_dir, ex, vt) for ex in older_sel]

    recent_json: dict[str, Any] = {}
    older_json: dict[str, Any] = {}
    warn_recent = False
    warn_older = False
    warn_syn = False

    if recent_payloads:
        pr = insight_prompt_partial(display_name, pct, leg_avg, re_avg, recent_payloads)
        recent_json, warn_recent = await call_ollama_json_validated(
            session, model, vt, pr, 1, timeout=300
        )
    else:
        logger.info("No recent examples for %s — skipping recent LLM call", vt)

    if older_payloads:
        po = insight_prompt_partial(display_name, pct, leg_avg, re_avg, older_payloads)
        older_json, warn_older = await call_ollama_json_validated(
            session, model, vt, po, 2, timeout=300
        )
    else:
        logger.info("No older examples for %s — skipping older LLM call", vt)

    final: dict[str, Any] = {}
    if recent_json and older_json:
        p_syn = build_synthesize_prompt_body(display_name, recent_json, older_json)
        final, warn_syn = await call_ollama_json_validated(
            session, model, vt, p_syn, 3, timeout=300
        )
    elif recent_json:
        final = recent_json
    elif older_json:
        final = older_json
    else:
        final = {
            "pattern_summary": "Insufficient examples with LLM-reported violations for this type.",
            "student_misconception": "",
            "teaching_suggestion": "",
            "legacy_vs_reimplementation_note": "",
        }

    off_topic_warning = bool(warn_recent or warn_older or warn_syn)

    # Examples for output: deduped union of recent + older payloads
    ex_seen: set[str] = set()
    examples_out: list[dict] = []
    for e in recent_payloads + older_payloads:
        key = f"{e['project_id']}:{json.dumps(e['violation_detail'], sort_keys=True)}"
        if key not in ex_seen:
            ex_seen.add(key)
            examples_out.append(e)

    return {
        "violation_type": vt,
        "display_name": display_name,
        "total_occurrences": total_occ,
        "pct_projects_affected": round(pct, 2),
        "legacy_avg": round(leg_avg, 3),
        "reimplementation_avg": round(re_avg, 3),
        "examples": examples_out,
        "pattern_summary": final.get("pattern_summary", ""),
        "student_misconception": final.get("student_misconception", ""),
        "teaching_suggestion": final.get("teaching_suggestion", ""),
        "legacy_vs_reimplementation_note": final.get("legacy_vs_reimplementation_note", ""),
        "off_topic_warning": off_topic_warning,
        "off_topic_warnings": {
            "recent_partial": warn_recent,
            "older_partial": warn_older,
            "synthesize": warn_syn,
        },
        "_partial_recent": recent_json,
        "_partial_older": older_json,
    }


def strip_internal_fields(insight: dict) -> dict:
    out = {k: v for k, v in insight.items() if not k.startswith("_")}
    return out


async def main_async(args: argparse.Namespace) -> None:
    paths = sorted(glob.glob(args.input))
    if not paths:
        paths = [args.input] if Path(args.input).exists() else []
    if not paths:
        logger.error("No evaluation files matched: %s", args.input)
        sys.exit(1)

    records: list[dict] = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not records:
        logger.error("No records loaded")
        sys.exit(1)

    years = [normalize_semester_year(r.get("semester") or "") for r in records]
    years_clean = [y for y in years if y is not None]
    date_min = min(years_clean) if years_clean else None
    date_max = max(years_clean) if years_clean else None

    cache_dir = Path(args.cache)

    violation_types = [args.violation] if args.violation else VIOLATION_TYPES

    insights: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for vt in violation_types:
            logger.info("Processing %s ...", vt)
            ins = await run_insight_for_type(session, args.model, vt, records, cache_dir)
            insights.append(strip_internal_fields(ins))

    insights.sort(key=lambda x: -x.get("pct_projects_affected", 0))

    out_doc = {
        "summary": {
            "total_projects": len(records),
            "date_range": {"min_year": date_min, "max_year": date_max},
            "violation_types_analyzed": violation_types,
            "source_files": paths,
        },
        "insights": insights,
    }

    out_path = Path(args.output)
    out_path.write_text(json.dumps(out_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote %s", out_path)

    print("\n" + "=" * 60)
    print("TEACHING INSIGHTS SUMMARY")
    print("=" * 60)
    print(f"Total projects: {len(records)}")
    print(f"Years covered: {date_min} – {date_max}")
    print(f"Violation types: {', '.join(violation_types)}")
    print()
    for ins in insights:
        dn = ins.get("display_name", "")
        pct = ins.get("pct_projects_affected", 0)
        leg = ins.get("legacy_avg", 0)
        reim = ins.get("reimplementation_avg", 0)
        print(f"=== {dn} ===")
        print(f"  Affected {pct}% of projects")
        print(f"  Legacy avg: {leg} | Reimplementation avg: {reim}")
        print(f"  Pattern: {ins.get('pattern_summary', '')}")
        print(f"  Misconception: {ins.get('student_misconception', '')}")
        print(f"  Teaching suggestion: {ins.get('teaching_suggestion', '')}")
        print(f"  Legacy vs Reimplementation: {ins.get('legacy_vs_reimplementation_note', '')}")
        if ins.get("off_topic_warning"):
            print("  [NOTE] off_topic_warning: LLM response may be off-topic (see off_topic_warnings)")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate teaching insights from evaluation JSONL")
    parser.add_argument("--input", "-i", type=str, default="evaluations_v4-*.jsonl")
    parser.add_argument("--cache", "-c", type=Path, default=Path("pr_cache"))
    parser.add_argument("--output", "-o", type=Path, default=Path("teaching_insights.json"))
    parser.add_argument(
        "--model",
        "-m",
        type=str,
        default="deepseek-coder-v2:16b-lite-instruct-q4_K_M",
    )
    parser.add_argument(
        "--violation",
        type=str,
        default=None,
        metavar="TYPE",
        choices=VIOLATION_TYPES,
        help="Only process this violation type (e.g. feature_envy)",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
