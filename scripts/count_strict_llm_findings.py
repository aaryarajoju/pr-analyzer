#!/usr/bin/env python3
"""
Count LLM finding entries in evaluations_v4-*.jsonl that pass a *stricter* screen than
heuristic junk removal alone. Reproduces numbers in violation_strict_counts.md.

This is still NOT human-labeled "ground truth" — it filters obvious garbage and weak anchors.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

VIOLATION_TYPES = [
    "srp",
    "dry",
    "lod",
    "long_chain",
    "cmo",
    "lsp",
    "god_object",
    "feature_envy",
    "long_method",
    "shotgun_surgery",
    "ocp",
    "dip",
    "information_expert",
]

GENERIC_CLASS = re.compile(
    r"\b(Square|PaymentProcessor|UserManager|OrderController|OrderService|OrderPrinter|"
    r"UtilityHelper|UtilityClass|ReportGenerator|InstitutionManager|Shape|MainService|"
    r"UserService)\b",
    re.I,
)

ABSTRACT_REASON = re.compile(
    r"(method\s+[XYZ]\s+|\bobject\s+[XYZ]\b|calls\s+method\s+[XYZ]|"
    r"method\s+A\s+calls\s+method\s+B)",
    re.I,
)

PATH_OR_FILE = re.compile(
    r"(app/|config/|db/|lib/|spec/|test/|src/|components/|"
    r"[A-Za-z0-9_./\-]+\.(rb|erb|haml|tsx|ts|jsx|vue|js)\b)",
)

PASCAL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b")


def blob(v: dict) -> str:
    return json.dumps(v, default=str)


def extract_violations(lf: dict | None) -> list[dict]:
    out: list[dict] = []
    if not isinstance(lf, dict):
        return out
    for vt in VIOLATION_TYPES:
        block = lf.get(vt) or {}
        for v in block.get("violations") or []:
            if isinstance(v, dict):
                out.append({"type": vt, **v})
    return out


def is_junk(v: dict) -> bool:
    b = blob(v).lower()
    c = str(v.get("class") or "")
    loc = str(v.get("location") or "")
    concern = str(v.get("concern") or "")
    reason = str(v.get("reason") or "")
    files = v.get("files_affected") or []
    files_s = " ".join(str(f) for f in files) if isinstance(files, list) else ""

    if concern.strip() == "X" and "y" in files_s.lower():
        return True
    if reason.strip() == "Z":
        return True
    if concern == "X" and len(files) == 1 and str(files[0]) == "Y":
        return True
    if re.search(r"\bgemfile(\.lock)?\b", c, re.I):
        return True
    if c.lower() == "square" and "setwidth" in b.replace(" ", ""):
        return True
    if re.search(r"\ba\.b\.c\.d", loc + reason):
        return True
    if re.match(r"^a\.set_user", loc):
        return True
    gen = {"model.rb", "controller.rb", "view.rb", "serializer.rb"}
    if files and all(str(f).split("/")[-1] in gen for f in files) and len(files) >= 4:
        if not any("app/" in str(f) for f in files):
            return True
    return False


def has_app_path(v: dict) -> bool:
    return "app/" in blob(v)


def is_generic_no_app(v: dict) -> bool:
    """
    True → candidate drop as textbook-style class name *only if* this finding has no ``app/``.

    If ``app/`` appears anywhere in the finding JSON, returns False (row is **not** dropped
    by this rule). Used by ``strict_pass`` only; ``moderate_pass`` does not call this.
    """
    if has_app_path(v):
        return False
    return bool(GENERIC_CLASS.search(blob(v)))


def has_grounding_signal(v: dict) -> bool:
    """Path/file snippet, Ruby/JS idiom, or non-blacklisted CamelCase token in JSON."""
    b = blob(v)
    if PATH_OR_FILE.search(b):
        return True
    if "@" in b or "::" in b:
        return True
    for m in PASCAL.finditer(b):
        word = m.group(0)
        if GENERIC_CLASS.search(word):
            continue
        if re.match(r"^(Order|User|Payment)(Controller|Service|Printer)?$", word):
            continue
        if len(word) >= 6:
            return True
    return False


def has_loose_grounding_signal(v: dict) -> bool:
    """Like strict grounding, but also pass if class, location, or chain is non-empty (short rationales often omit paths)."""
    if has_grounding_signal(v):
        return True
    if str(v.get("class") or "").strip():
        return True
    if str(v.get("location") or "").strip():
        return True
    if str(v.get("chain") or "").strip():
        return True
    if PASCAL.search(blob(v)):
        return True
    return False


def moderate_pass(v: dict) -> bool:
    """Stricter than junk-only; looser than strict_pass (see violation_strict_counts.md).

    Does **not** apply ``is_generic_no_app``: any finding with a real ``app/`` path is already
    never hit by that rule, and many valid rows only name a class without repeating ``app/``.
    Moderate counts all of those; only obvious ``is_junk`` rows are removed.
    """
    if is_junk(v):
        return False
    if not has_loose_grounding_signal(v):
        return False
    if len(str(v.get("reason") or "").strip()) < 12:
        return False
    r = str(v.get("reason") or "")
    loc = str(v.get("location") or "")
    if ABSTRACT_REASON.search(r) or ABSTRACT_REASON.search(loc):
        return False
    c = str(v.get("class") or "").strip()
    if re.match(r"^X\d+$", c, re.I) or c in {"52", "A", "B"}:
        return False
    return True


def strict_pass(v: dict) -> bool:
    if is_junk(v) or is_generic_no_app(v):
        return False
    if not has_grounding_signal(v):
        return False
    if len(str(v.get("reason") or "").strip()) < 25:
        return False
    r = str(v.get("reason") or "")
    loc = str(v.get("location") or "")
    if ABSTRACT_REASON.search(r) or ABSTRACT_REASON.search(loc):
        return False
    c = str(v.get("class") or "").strip()
    if re.match(r"^X\d+$", c, re.I) or c in {"52", "A", "B"}:
        return False
    return True


def moderate_fail_reason(v: dict) -> str:
    """Why ``moderate_pass`` is False; empty string if it passes."""
    if moderate_pass(v):
        return ""
    if is_junk(v):
        return "junk_or_placeholder"
    if not has_loose_grounding_signal(v):
        return "no_grounding"
    if len(str(v.get("reason") or "").strip()) < 12:
        return "reason_too_short"
    r = str(v.get("reason") or "")
    loc = str(v.get("location") or "")
    if ABSTRACT_REASON.search(r) or ABSTRACT_REASON.search(loc):
        return "abstract_template"
    c = str(v.get("class") or "").strip()
    if re.match(r"^X\d+$", c, re.I) or c in {"52", "A", "B"}:
        return "placeholder_class"
    return "unknown"


def strict_fail_reason(v: dict) -> str:
    """Why ``strict_pass`` is False; empty string if it passes."""
    if strict_pass(v):
        return ""
    if is_junk(v):
        return "junk_or_placeholder"
    if is_generic_no_app(v):
        return "textbook_class_without_app_path"
    if not has_grounding_signal(v):
        return "weak_grounding"
    if len(str(v.get("reason") or "").strip()) < 25:
        return "reason_too_short"
    r = str(v.get("reason") or "")
    loc = str(v.get("location") or "")
    if ABSTRACT_REASON.search(r) or ABSTRACT_REASON.search(loc):
        return "abstract_template"
    c = str(v.get("class") or "").strip()
    if re.match(r"^X\d+$", c, re.I) or c in {"52", "A", "B"}:
        return "placeholder_class"
    return "unknown"


def finding_assessment(v: dict) -> dict[str, str | bool]:
    """
    Automated legitimacy labels for a single ``violations[]`` dict (not human review).

    - ``legitimate_moderate`` / ``legitimate_strict``: pipeline pass flags.
    - ``legitimacy_summary``: one-line text for CSV/reports.
    """
    m = moderate_pass(v)
    s = strict_pass(v)
    if m and s:
        summary = "Plausible (passes moderate + strict automated filters)"
    elif m:
        summary = "Plausible (passes moderate only; fails strict automated filters)"
    else:
        summary = "Not plausible under automated rules — " + moderate_fail_reason(v).replace("_", " ")
    return {
        "legitimate_moderate": m,
        "legitimate_strict": s,
        "moderate_fail_code": moderate_fail_reason(v) or "",
        "strict_fail_code": strict_fail_reason(v) or "",
        "legitimacy_summary": summary,
    }


def load_all_records(root: Path) -> list[dict]:
    recs: list[dict] = []
    for y in range(2016, 2026):
        p = root / f"evaluations_v4-{y}.jsonl"
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                recs.append(json.loads(line))
    return recs


def main() -> None:
    ap = argparse.ArgumentParser(description="Count filtered LLM findings (moderate vs strict).")
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repo root (default: pr-analyzer)",
    )
    ap.add_argument(
        "--tier",
        choices=("moderate", "strict", "both"),
        default="both",
        help="Which rule tier to print (default: both).",
    )
    args = ap.parse_args()
    root: Path = args.root

    recs = load_all_records(root)
    all_v: list[dict] = []
    for r in recs:
        all_v.extend(extract_violations(r.get("llm_findings")))

    n = len(all_v)
    m = sum(1 for v in all_v if moderate_pass(v))
    s = sum(1 for v in all_v if strict_pass(v))
    print(f"Parsed LLM finding entries: {n}")
    if args.tier in ("moderate", "both"):
        print(f"Moderate-pass count: {m} ({m / n:.1%})")
    if args.tier in ("strict", "both"):
        print(f"Strict-pass count:   {s} ({s / n:.1%})")


if __name__ == "__main__":
    main()
