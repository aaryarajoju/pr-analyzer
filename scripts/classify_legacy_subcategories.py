#!/usr/bin/env python3
"""
Classify Expertiza legacy (project_category == refactoring) projects into:
  - testing   — if the **official project title** contains the word "testing"
  - feature_add — else, keyword score favors new functionality / enhancements
  - refactoring — else, default / cleanup / bugfix language

**Official title** (in order):
  1. ``Project name`` from ``proj.csv`` (course spreadsheet), when present
  2. Title decoded from ``wiki.expertiza.ncsu.edu/...title=...`` URLs in wiki text
  3. ``extract_project_name()`` (wiki + diff heuristics)
  4. First line of project_feedback / design_feedback

Original ``evaluations_v4-*.jsonl`` files are not modified; copies are written as
``evaluations_v4_classified-*.jsonl``.

Feature vs refactor (non-testing):
  Compare keyword scores on combined wiki + feedback + diff (length-normalized).
  Boilerplate phrases like “project overview” are excluded from feature signals.
  “Improve”, “cleanup”, “existing code”, etc. boost refactoring.
  Small margins get **low** confidence but the **higher score still wins** (no forced tie-break).

**Why counts may not match a manual "55":** the spreadsheet must list every project ID;
duplicate rows in ``proj.csv`` keep the **last** ``Project name``. Projects missing from
``proj.csv`` use wiki/feedback fallbacks, which may not include "testing" in the title
string even for testing-focused OSS work—curate ``proj.csv`` or adjust fallbacks.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import pandas as pd

# --- Keyword groups for feature_add vs refactoring only ----------------------------
# Feature: *new* capability / product-facing additions (narrow — avoid wiki boilerplate).
# Refactor: cleanup, improvement of existing code, fixes, “improve X” language.

_FEATURE = [
    r"\bnew feature\b",
    r"\benhancement\b",
    r"user stor(?:y|ies)",
    r"add(?:ing)? (?:a |the )?(?:feature|capability|functionality)",
    r"new functionality",
    r"\bintroduce\s+(?:a |the )?(?:new |)",
    r"\bimplement(?:ing|ation)?\s+(?:a |the )?(?:new |additional )",
    r"\bimplement(?:ing|ation)?\s+support\s+for\b",
    r"\bheatmap\b",
    r"heatgrid",
    r"assign reviewer",
    r"weights in grade",
    r"grade calculation",
    r"quiz questionnaire",
    r"\bnew user\b",
    r"new workflow",
    r"new page",
    r"new interface",
    r"\badd(?:ing)?\s+(?:a |the )?(?:field|button|column|tab|view|page|endpoint)\b",
    # Product / integration language (not wiki boilerplate)
    r"\bintegrat(?:e|ing)\s+(?:with|into|a |the |Simicheck|GitHub|github)",
    r"\bweb\s+service\b",
    r"\ballow\s+(?:users|reviewers|instructors|students|the\s+user|the\s+reviewer)\b",
    r"\ballow\s+(?:a |the )?(?:reviewer|instructor|student)\s+to\b",
    r"\bnew\s+(?:view|tab|panel|screen|report|dashboard|endpoint|ability|option)\b",
    r"\bdrag[- ]?(?:and[- ]?)?drop\b",
    r"\bvisualization\b",
    r"\bexport\s+(?:review|scores|grades|data)\b",
    r"\bauto[- ]?generat",
    r"\bcalibration\s+(?:submission|assignment)\b",
    r"\bmetrics\s+integration\b",
    r"\bbadging?\b",
    r"\b(?:self[- ]?review|peer[- ]?review)\s+feature\b",
]

_REFACTOR = [
    r"\brefactor(?:ing)?\b",
    r"extract (?:method|class|module)",
    r"clean(?:ing)? up (?:the )?code",
    r"\bcleanup\b",
    r"clean up",
    r"technical debt",
    r"code smell",
    r"restructure",
    r"reduce duplication",
    r"\bdry\b",
    r"design pattern",
    r"\bimprove(?:ment|s)\b",
    r"\bimprove\b",
    r"\bimproved\b",
    r"\bimproving\s+(?:the\s+)?(?:code|structure|design|readability|maintainability)\b",
    r"\bstreamline\b",
    r"\bsimplif(?:y|ying|ication)\b",
    r"\bconsolidat(?:e|ing|ion)\b",
    r"\boptimize\b",
    r"\breadability\b",
    r"\bmaintainability\b",
    r"existing (?:code|implementation|method|class|behavior)",
    r"current (?:implementation|code|behavior)",
    r"issue description",
    r"existing issue",
    r"bug fix",
    r"fixing (?:a |the )?bug",
    r"\bfix(?:es|ed|ing)?\s+(?:the |a )?(?:bug|issue|error|problem)\b",
    r"cannot (?:begin|view|edit)",
    r"meta-review",
    r"workaround",
    r"\bmodify(?:ing)?\s+(?:the |existing )\b",
    r"\bre(?:place|move) (?:duplicate|redundant|obsolete)\b",
]

_FEATURE_RES = [re.compile(p, re.I) for p in _FEATURE]
_REFACTOR_RES = [re.compile(p, re.I) for p in _REFACTOR]

_TESTING_TITLE_WORD = re.compile(r"\btesting\b", re.I)


def load_project_registry(csv_path: Path) -> dict[str, str]:
    """
    project_id -> Project name (last row wins if duplicates).
    """
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, dtype=str)
    if "Project ID" not in df.columns or "Project name" not in df.columns:
        return {}
    out: dict[str, str] = {}
    for _, row in df.iterrows():
        pid = str(row.get("Project ID") or "").strip()
        if not re.match(r"^E\d+$", pid, re.I):
            continue
        name = str(row.get("Project name") or "").strip()
        if name:
            out[pid] = name
    return out


def extract_title_from_wiki_url(wiki_text: str) -> str:
    """Decode Expertiza wiki URL title=... segment into a readable string."""
    if not wiki_text:
        return ""
    m = re.search(
        r"wiki\.expertiza\.ncsu\.edu/index\.php\?title=([^\s\"'<]+)",
        wiki_text,
        re.I,
    )
    if not m:
        return ""
    raw = unquote(m.group(1).replace("_", " "))
    # Typical: CSC/ECE 517 Fall 2025 - E2529. Testing for the Questionnaire Helper...
    m2 = re.search(r"E\d{4}\.\s*(.+)", raw, re.I)
    if m2:
        return m2.group(1).strip()[:240]
    return raw[:240]


def extract_project_name(wiki_text: str | None, project_id: str, diff_prefix: str = "") -> str:
    """Heuristic short label from wiki/diff (kept for fallback titles)."""
    t = str(wiki_text or "").replace("\r", "\n")
    pid = (project_id or "").strip()

    if pid:
        m = re.search(rf"{re.escape(pid)}\s*[-–—]\s*([^\n]+)", t, re.I)
        if m:
            title = m.group(1).strip()
            if len(title) > 2:
                return title[:220]

    m = re.search(r"E\d{4}\s*[-–—]\s*([^\n]+)", t)
    if m:
        title = m.group(1).strip()
        if len(title) > 2:
            return title[:220]

    m = re.search(r"(?:^|\n)\s*Issue\s*1\s*[-–—]\s*([^\n]+)", t, re.I)
    if m:
        return ("Issue: " + m.group(1).strip())[:220]

    for hdr in (
        "Overview of Project",
        "Project Overview",
        "Problem Statement and Background",
        "Problem Statement",
        "The goal behind this project",
        "Background",
    ):
        m = re.search(rf"(?:^|\n)\s*{re.escape(hdr)}\s*\n+", t, re.I)
        if m:
            for line in t[m.end() : m.end() + 1500].split("\n"):
                line = line.strip()
                if re.match(r"^\d+$", line):
                    continue
                if len(line) >= 70:
                    return line[:220]

    for _pd in re.finditer(r"(?:^|\n)\s*Problem Description\s*\n+", t, re.I):
        if _pd.start() < 400:
            continue
        for line in t[_pd.end() : _pd.end() + 1500].split("\n"):
            line = line.strip()
            if re.match(r"^\d+$", line):
                continue
            if len(line) >= 70:
                return line[:220]

    for _m in re.finditer(r"(?is)(?:^|\n)\s*Project Outline\s*\n", t):
        chunk = t[_m.end() : _m.end() + 1200]
        for line in chunk.split("\n"):
            line = line.strip()
            if not line:
                continue
            if re.match(r"^\d+(\.\d+)*$", line):
                continue
            if len(line) <= 25:
                continue
            if len(line) < 50 and line.istitle() and line.count(" ") <= 4 and "." not in line:
                continue
            if len(line) >= 45:
                return line[:220]

    if pid and diff_prefix:
        dp = str(diff_prefix)
        m = re.search(rf"^\+\s*#+\s*{re.escape(pid)}\s*[-–—]\s*([^\n]+)", dp, re.MULTILINE | re.I)
        if m:
            title = m.group(1).strip()
            if len(title) > 2:
                return title[:220]
        m = re.search(rf"^\+\s*#+\s*{re.escape(pid)}\s+([^\n]+)", dp, re.MULTILINE | re.I)
        if m:
            title = m.group(1).strip()
            if len(title) > 2:
                return title[:220]

    return ""


def feedback_line_fallback(row: dict) -> str:
    for key in ("project_feedback", "design_feedback"):
        v = row.get(key)
        if not v or not str(v).strip():
            continue
        line = str(v).strip().split("\n")[0].strip()
        if len(line) < 12:
            continue
        if (line.startswith('"') and line.endswith('"')) or (line.startswith("'") and line.endswith("'")):
            line = line[1:-1].strip()
        return line[:200]
    return ""


def canonical_title(
    pid: str,
    row: dict,
    registry: dict[str, str],
) -> tuple[str, str]:
    """
    Returns (title_string, source_label).
    """
    pid_key = (pid or "").strip()
    if pid_key in registry:
        return registry[pid_key], "proj_csv"

    wiki = str(row.get("wiki_content") or "")
    diff_p = str(row.get("diff_prefix") or "")[:12000]

    url_t = extract_title_from_wiki_url(wiki)
    if url_t.strip():
        return url_t.strip(), "wiki_url"

    ex = extract_project_name(wiki, pid_key, diff_p)
    if ex.strip():
        return ex.strip(), "wiki_extract"

    fb = feedback_line_fallback(row)
    if fb.strip():
        return fb.strip(), "feedback_first_line"

    return "", "empty"


def title_indicates_testing(title: str) -> bool:
    return bool(title and _TESTING_TITLE_WORD.search(title))


def _score(res_list: list[re.Pattern], text: str) -> float:
    if not text:
        return 0.0
    return float(sum(len(rx.findall(text)) for rx in res_list))


def classify_feature_vs_refactor(text: str) -> tuple[str, str, dict[str, float], str]:
    """When not testing: pick feature_add or refactoring from keyword scores."""
    if not text or len(text.strip()) < 40:
        return (
            "refactoring",
            "low",
            {"feature_add": 0.0, "refactoring": 0.0},
            "insufficient text; defaulted to refactoring",
        )

    sf = _score(_FEATURE_RES, text)
    sr = _score(_REFACTOR_RES, text)
    n = max(len(text), 1)
    ln = math.log10(n / 500 + 1)
    sf_n = sf / (1.0 + 0.25 * ln)
    sr_n = sr / (1.0 + 0.25 * ln)

    scores = {"feature_add": sf_n, "refactoring": sr_n}
    margin = abs(sf_n - sr_n)

    if sf_n > sr_n:
        cat = "feature_add"
        rationale = f"feature_add score {sf_n:.2f} vs refactoring {sr_n:.2f}"
    elif sr_n > sf_n:
        cat = "refactoring"
        rationale = f"refactoring score {sr_n:.2f} vs feature_add {sf_n:.2f}"
    else:
        cat = "refactoring"
        rationale = "tie → refactoring (default)"

    if margin >= 1.5:
        conf = "high"
    elif margin >= 0.6:
        conf = "medium"
    else:
        conf = "low"
        rationale = rationale + " (low margin)"

    return cat, conf, scores, rationale


def load_dataset_year(dataset_path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not dataset_path.exists():
        return out
    with dataset_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            pid = r.get("project_id")
            if not pid:
                continue
            diff = r.get("diff") or ""
            if isinstance(diff, str) and len(diff) > 4000:
                diff = diff[:4000]
            out[pid] = {
                "wiki_content": r.get("wiki_content"),
                "project_feedback": r.get("project_feedback"),
                "design_feedback": r.get("design_feedback"),
                "diff_prefix": diff,
            }
    return out


def build_context(pid: str, ds: dict[str, dict]) -> tuple[str, str]:
    row = ds.get(pid) or {}
    parts: list[str] = []
    w = row.get("wiki_content")
    if w and str(w).strip():
        parts.append(str(w))
    pf = row.get("project_feedback")
    if pf and str(pf).strip():
        parts.append("--- project_feedback ---\n" + str(pf))
    df = row.get("design_feedback")
    if df and str(df).strip():
        parts.append("--- design_feedback ---\n" + str(df))
    dp = row.get("diff_prefix")
    if dp and str(dp).strip():
        parts.append("--- diff (prefix) ---\n" + str(dp)[:6000])

    if not parts:
        return "", "minimal_context"

    text = "\n\n".join(parts)
    if w and str(w).strip():
        src = "wiki+feedback+diff" if len(parts) > 1 else "wiki"
    else:
        src = "feedback+diff" if len(parts) > 1 else "feedback_only"
    return text, src


def process_year(
    year: int,
    root: Path,
    dry_run: bool,
    registry: dict[str, str],
) -> list[dict]:
    ev_path = root / f"evaluations_v4-{year}.jsonl"
    ds_path = root / f"dataset-{year}.jsonl"
    out_path = root / f"evaluations_v4_classified-{year}.jsonl"

    if not ev_path.exists():
        print(f"Skip {year}: missing {ev_path.name}")
        return []

    ds = load_dataset_year(ds_path)
    summary_rows: list[dict] = []
    out_lines: list[str] = []

    with ev_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            pc = (rec.get("project_category") or "").lower()
            if pc != "refactoring":
                out_lines.append(json.dumps(rec, ensure_ascii=False))
                continue

            pid = rec.get("project_id") or ""
            text, src = build_context(pid, ds)
            row = ds.get(pid) or {}

            title, title_src = canonical_title(pid, row, registry)
            display_name = title
            if len(display_name) > 140:
                display_name = display_name[:137] + "..."

            if title_indicates_testing(title):
                cat = "testing"
                conf = "high"
                scores = {"feature_add": 0.0, "refactoring": 0.0}
                rationale = f'word "testing" in official title (source={title_src})'
            else:
                cat, conf, scores, rationale = classify_feature_vs_refactor(text)

            rec["legacy_subcategory"] = cat
            rec["legacy_subcategory_confidence"] = conf
            rec["legacy_subcategory_rationale"] = rationale
            rec["legacy_subcategory_scores"] = scores
            rec["legacy_subcategory_text_source"] = src
            rec["legacy_subcategory_project_title"] = title
            rec["legacy_subcategory_title_source"] = title_src
            rec["legacy_subcategory_project_name"] = title

            out_lines.append(json.dumps(rec, ensure_ascii=False))
            summary_rows.append(
                {
                    "year": year,
                    "project_id": pid,
                    "project_name": display_name,
                    "title_source": title_src,
                    "semester": rec.get("semester"),
                    "legacy_subcategory": cat,
                    "confidence": conf,
                    "text_source": src,
                    "rationale": rationale[:240],
                }
            )

    if not dry_run:
        out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"Wrote {out_path} ({len(out_lines)} lines)")

    return summary_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify legacy refactoring projects into subcategories")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="pr-analyzer repo root",
    )
    parser.add_argument("--years", type=str, default="2016-2025")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--project-registry",
        type=Path,
        default=None,
        help="CSV with Project ID and Project name (default: <root>/proj.csv)",
    )
    args = parser.parse_args()

    root = args.root
    reg_path = args.project_registry if args.project_registry is not None else root / "proj.csv"
    registry = load_project_registry(reg_path)
    print(f"Loaded {len(registry)} project titles from {reg_path}")

    years: list[int] = []
    if "-" in args.years and "," not in args.years:
        a, b = args.years.split("-", 1)
        years = list(range(int(a), int(b) + 1))
    else:
        years = [int(x.strip()) for x in args.years.split(",") if x.strip()]

    all_summary: list[dict] = []
    for y in years:
        all_summary.extend(process_year(y, root, args.dry_run, registry))

    csv_path = root / "legacy_subcategory_classifications.csv"
    if not args.dry_run and all_summary:
        fieldnames = list(all_summary[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as cf:
            w = csv.DictWriter(cf, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(all_summary)
        print(f"Wrote {csv_path} ({len(all_summary)} legacy rows)")

    c = Counter(r["legacy_subcategory"] for r in all_summary)
    print("Counts by subcategory:", dict(c))
    testing_titles = sum(
        1
        for r in all_summary
        if r["legacy_subcategory"] == "testing" and (r.get("title_source") == "proj_csv")
    )
    print(f"Testing rows with title from proj.csv: {testing_titles}")


if __name__ == "__main__":
    main()
