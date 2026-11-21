#!/usr/bin/env python3
"""
Regenerate violation_sample_238_list.csv and .md (25 projects, random_state=42)
with automated legitimacy columns from count_strict_llm_findings.finding_assessment.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load_sf():
    p = ROOT / "scripts" / "count_strict_llm_findings.py"
    spec = importlib.util.spec_from_file_location("count_strict_llm_findings", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


sf = _load_sf()

VIOLATION_TYPES = sf.VIOLATION_TYPES


def normalize_semester(semester: str) -> str:
    if not semester or not isinstance(semester, str):
        return ""
    s = str(semester).strip()
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s).strip()
    m = re.search(r"(\d{4})\s+(Fall|Spring)", s, re.I)
    if m:
        year, season = int(m.group(1)), m.group(2).capitalize()
    else:
        m = re.search(r"(Fall|Spring)\s+(\d{4})", s, re.I)
        if m:
            season, year = m.group(1).capitalize(), int(m.group(2))
        else:
            return str(semester)
    return f"{season} {year}"


def extract_violations(lf):
    return sf.extract_violations(lf)


def one_line(v: dict) -> str:
    parts = []
    for k in ("class", "location", "method", "chain", "reason", "concern"):
        if v.get(k):
            parts.append(f"{k}={str(v[k])[:220]}")
    return " | ".join(parts) if parts else json.dumps(v, default=str)[:320]


def main() -> None:
    all_recs = []
    for y in range(2016, 2026):
        p = ROOT / f"evaluations_v4-{y}.jsonl"
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                all_recs.append(json.loads(line))

    index = {
        (str(r.get("project_id", "")).strip(), normalize_semester(str(r.get("semester", "")))): r
        for r in all_recs
    }

    df = pd.read_csv(ROOT / "insights_v4.csv")
    sample = df.sample(25, random_state=42)

    rows = []
    idx = 0
    for _, row in sample.iterrows():
        pid = str(row["project_id"]).strip()
        sem = normalize_semester(str(row["semester"]))
        r = index.get((pid, sem))
        if not r:
            continue
        for v in extract_violations(r.get("llm_findings") or {}):
            idx += 1
            a = sf.finding_assessment(v)
            rows.append(
                {
                    "n": idx,
                    "project_id": pid,
                    "semester": row["semester"],
                    "eval_semester": r.get("semester"),
                    "repo": r.get("repo_name"),
                    "category": r.get("project_category"),
                    "violation_type": v.get("type"),
                    "details": one_line(v),
                    "legitimate_moderate": a["legitimate_moderate"],
                    "legitimate_strict": a["legitimate_strict"],
                    "moderate_fail_code": a["moderate_fail_code"],
                    "strict_fail_code": a["strict_fail_code"],
                    "legitimacy_summary": a["legitimacy_summary"],
                }
            )

    out_csv = ROOT / "violation_sample_238_list.csv"
    out_md = ROOT / "violation_sample_238_list.md"
    pd.DataFrame(rows).to_csv(out_csv, index=False)

    lines = [
        "# All 238 LLM findings — 25-project sample (`random_state=42`)",
        "",
        "One row per `llm_findings` → `violations[]` entry. **Legitimacy columns are automated**",
        "(see `scripts/count_strict_llm_findings.py`); they are **not** manual code review.",
        "",
        "| # | project_id | semester | repo | type | legitimate (moderate) | legitimate (strict) | assessment | details |",
        "|---|------------|----------|------|------|:---------------------:|:-------------------:|------------|---------|",
    ]
    for r in rows:
        det = r["details"].replace("|", "\\|")
        lines.append(
            f"| {r['n']} | {r['project_id']} | {r['semester']} | {r['repo']} | {r['violation_type']} | "
            f"{r['legitimate_moderate']} | {r['legitimate_strict']} | {r['legitimacy_summary'].replace('|', ';')} | {det} |"
        )

    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_csv} and {out_md} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
