#!/usr/bin/env python3
"""
Analyze evaluation results from evaluations_v3-*.jsonl.

Replaces analyze_evaluations.py for the v3 output format.

Usage:
    python analyze_evaluations_v3.py
    python analyze_evaluations_v3.py --input "evaluations_v3-*.jsonl" --export insights_v3.csv
    python analyze_evaluations_v3.py --year 2023 --semester "Spring 2025"
"""

import argparse
import glob
import json
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from scipy.stats import pearsonr

VIOLATION_TYPES = [
    "srp", "dry", "lod", "long_chain", "cmo", "lsp",
    "god_object", "feature_envy", "long_method", "shotgun_surgery", "ocp",
    "dip", "information_expert",
]


def load_evaluations(input_pattern: str) -> list[dict]:
    """Load all evaluation files matching pattern."""
    records = []
    if "*" in input_pattern:
        paths = sorted(Path(".").glob(input_pattern))
    else:
        paths = [Path(p) for p in glob.glob(input_pattern)]
        if not paths and Path(input_pattern).exists():
            paths = [Path(input_pattern)]
    if not paths:
        paths = sorted(Path(".").glob("evaluations_v3-*.jsonl"))
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    records.append(r)
                except json.JSONDecodeError:
                    pass
    return records


def normalize_semester(semester: str) -> tuple[str, int]:
    """
    Normalize semester to "Season YYYY" format and compute sort_key.
    Ignores "Final", "OSS", parenthetical notes.
    sort_key = year * 10 + (1 if Fall else 2); Fall before Spring within same year.
    Returns (normalized_str, sort_key).
    """
    if not semester or not isinstance(semester, str):
        return ("", 0)
    s = semester.strip()
    # Remove parenthetical notes
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s).strip()

    year: Optional[int] = None
    season: Optional[str] = None

    # Try "YYYY Season" format (e.g. 2016 Fall Final, 2011 Fall OSS)
    m = re.search(r"(\d{4})\s+(Fall|Spring)", s, re.IGNORECASE)
    if m:
        year = int(m.group(1))
        season = m.group(2).capitalize()

    # Try "Season YYYY" format (e.g. Fall 2021 OSS, Spring 2025 Final)
    if year is None or season is None:
        m = re.search(r"(Fall|Spring)\s+(\d{4})", s, re.IGNORECASE)
        if m:
            season = m.group(1).capitalize()
            year = int(m.group(2))

    if year is None or season is None:
        return (semester, 0)

    normalized = f"{season} {year}"
    sort_key = year * 10 + (1 if season.lower() == "fall" else 2)
    return (normalized, sort_key)


def build_dataframe(records: list[dict]) -> pd.DataFrame:
    """Build DataFrame from v3 evaluation records."""
    rows = []
    for r in records:
        vc = r.get("violation_counts", {})
        sc = r.get("static_counts", {})
        raw_semester = r.get("semester", "")
        semester, sort_key = normalize_semester(raw_semester)
        parts = semester.split()
        season = parts[0] if len(parts) >= 1 else ""
        year = int(parts[1]) if len(parts) >= 2 else None
        row = {
            "project_id": r.get("project_id", ""),
            "semester": semester,
            "sort_key": sort_key,
            "season": season,
            "year": year,
            "repo_name": r.get("repo_name", ""),
            "project_type": r.get("project_type", ""),
            "project_category": r.get("project_category", ""),
            "total_llm_violations": vc.get("total", 0),
            "total_static_violations": sc.get("total", 0),
            "alignment_score": r.get("alignment_score"),
        }
        for vt in VIOLATION_TYPES:
            row[f"{vt}_llm"] = vc.get(vt, 0)
            row[f"{vt}_static"] = sc.get(vt, 0)
        rows.append(row)
    return pd.DataFrame(rows)


def filter_records(
    df: pd.DataFrame,
    year_filter: Optional[list[int]] = None,
    semester_filter: Optional[str] = None,
) -> pd.DataFrame:
    """Filter by year and/or semester."""
    if year_filter is not None:
        df = df[df["year"].isin(year_filter)]
    if semester_filter is not None:
        df = df[df["semester"].astype(str).str.contains(semester_filter, case=False, na=False)]
    return df


def run_analysis(
    df: pd.DataFrame,
    records: list[dict],
) -> None:
    """Print all analysis sections."""
    n = len(df)
    if n == 0:
        print("No records to analyze.")
        return

    print("=" * 60)
    print("INSIGHTS: Design Violation Analysis (v3)")
    print("=" * 60)

    # 1. Overview
    total_llm = df["total_llm_violations"].sum()
    total_static = df["total_static_violations"].sum()
    avg_llm = df["total_llm_violations"].mean()
    avg_static = df["total_static_violations"].mean()
    avg_align = df["alignment_score"].mean()
    good_align = (df["alignment_score"] >= 4).sum()
    good_align_pct = 100 * good_align / n if n else 0

    print("\n## 1. Overview")
    print(f"  Total projects analyzed: {n}")
    print(f"  Total violations (LLM): {total_llm}")
    print(f"  Total violations (Static): {total_static}")
    print(f"  Avg violations per project (LLM): {avg_llm:.1f}")
    print(f"  Avg violations per project (Static): {avg_static:.1f}")
    print(f"  Average alignment score: {avg_align:.2f}")
    print(f"  Projects with alignment >= 4 (good alignment): {good_align} ({good_align_pct:.1f}%)")

    # 2. Per-violation-type breakdown
    print("\n## 2. Per-violation-type breakdown")
    llm_cols = [f"{vt}_llm" for vt in VIOLATION_TYPES]
    static_cols = [f"{vt}_static" for vt in VIOLATION_TYPES]
    rows = []
    for vt in VIOLATION_TYPES:
        llm_total = df[f"{vt}_llm"].sum()
        static_total = df[f"{vt}_static"].sum()
        llm_avg = df[f"{vt}_llm"].mean()
        static_avg = df[f"{vt}_static"].mean()
        try:
            a, b = df[f"{vt}_llm"], df[f"{vt}_static"]
            sa, sb = a.std(), b.std()
            if pd.isna(sa) or pd.isna(sb) or sa == 0 or sb == 0:
                corr = 0
            else:
                r, _ = pearsonr(a, b)
                corr = r if not (r != r) else 0  # handle NaN
        except Exception:
            corr = 0
        rows.append({
            "type": vt,
            "llm_total": llm_total,
            "llm_avg": llm_avg,
            "static_total": static_total,
            "static_avg": static_avg,
            "correlation": corr,
        })
    tbl = pd.DataFrame(rows)
    tbl = tbl.sort_values("llm_total", ascending=False)
    print(tbl.to_string(index=False))

    total_col = tbl["llm_total"] + tbl["static_total"]
    most_common = tbl.loc[total_col.idxmax(), "type"] if total_col.sum() > 0 else "N/A"
    print(f"\n  Most common violation type overall: {most_common}")

    # 3. By Project Category
    print("\n## 3. By Project Category")
    if "project_category" in df.columns and df["project_category"].notna().any():
        for cat in ["refactoring", "reimplementation", "unknown"]:
            cat_df = df[df["project_category"] == cat]
            if len(cat_df) == 0:
                continue
            n_cat = len(cat_df)
            avg_llm_cat = cat_df["total_llm_violations"].mean()
            avg_static_cat = cat_df["total_static_violations"].mean()
            avg_align_cat = cat_df["alignment_score"].mean()
            # Most common violation type (LLM + static combined)
            vt_totals = {vt: cat_df[f"{vt}_llm"].sum() + cat_df[f"{vt}_static"].sum() for vt in VIOLATION_TYPES}
            most_common = max(vt_totals, key=vt_totals.get) if sum(vt_totals.values()) > 0 else "N/A"
            # Breakdown: reimplementation_backend vs reimplementation_frontend
            backend_count = (cat_df["project_type"] == "reimplementation_backend").sum()
            frontend_count = (cat_df["project_type"] == "reimplementation_frontend").sum()
            print(f"\n  {cat}:")
            print(f"    project count: {n_cat}")
            print(f"    avg total LLM violations: {avg_llm_cat:.1f}")
            print(f"    avg total static violations: {avg_static_cat:.1f}")
            print(f"    avg alignment score: {avg_align_cat:.2f}")
            print(f"    most common violation type: {most_common}")
            if cat == "reimplementation":
                print(f"    breakdown: reimplementation_backend={backend_count}, reimplementation_frontend={frontend_count}")
    else:
        print("  No project_category data available.")

    # 4. Semester trends (chronological: Fall before Spring within same year)
    print("\n## 4. Semester trends")
    sem_order = df.groupby("semester").agg({"sort_key": "first"}).reset_index()
    sem_order = sem_order.sort_values("sort_key")
    semesters_ordered = sem_order["semester"].tolist()
    by_sem = df.groupby("semester").agg({
        "project_id": "count",
        "total_llm_violations": "mean",
        "total_static_violations": "mean",
        "alignment_score": "mean",
    }).round(2)
    by_sem.columns = ["project_count", "avg_llm", "avg_static", "avg_alignment"]
    by_sem = by_sem.reindex(semesters_ordered).dropna(how="all")
    for sem in by_sem.index:
        sem_df = df[df["semester"] == sem]
        top_type = max(VIOLATION_TYPES, key=lambda t: sem_df[f"{t}_llm"].sum())
        if sem_df[f"{top_type}_llm"].sum() == 0:
            top_type = "N/A"
        by_sem.loc[sem, "most_common_type"] = top_type
    print(by_sem.to_string())

    # 5. Top violators
    print("\n## 5. Top 10 projects by total LLM violations")
    top = df.nlargest(10, "total_llm_violations").copy()
    top["top_3_types"] = top.apply(
        lambda row: ", ".join(
            sorted(VIOLATION_TYPES, key=lambda vt: row[f"{vt}_llm"], reverse=True)[:3]
        ),
        axis=1,
    )
    cols = ["project_id", "semester", "total_llm_violations", "top_3_types"]
    print(top[cols].to_string(index=False))

    # 6. Violation co-occurrence matrix
    print("\n## 6. Violation co-occurrence (top 10 pairs)")
    pairs = []
    for i, vt1 in enumerate(VIOLATION_TYPES):
        for vt2 in VIOLATION_TYPES[i + 1 :]:
            both = ((df[f"{vt1}_llm"] > 0) & (df[f"{vt2}_llm"] > 0)).sum()
            pairs.append((vt1, vt2, both))
    pairs.sort(key=lambda x: x[2], reverse=True)
    for vt1, vt2, count in pairs[:10]:
        print(f"  {vt1} + {vt2}: {count} projects")

    # 7. Severity distribution
    print("\n## 7. Severity distribution")
    sev_counts = {1: 0, 2: 0, 3: 0}
    for r in records:
        lf = r.get("llm_findings", {})
        for vt in VIOLATION_TYPES:
            viols = lf.get(vt, {}).get("violations", [])
            for v in viols:
                s = v.get("severity")
                if s in (1, 2, 3):
                    sev_counts[s] = sev_counts.get(s, 0) + 1
    total_sev = sum(sev_counts.values())
    if total_sev > 0:
        for s in (1, 2, 3):
            c = sev_counts.get(s, 0)
            pct = 100 * c / total_sev
            print(f"  Severity {s}: {c} ({pct:.1f}%)")
    else:
        print("  No severity data available in violations.")

    # 8. LLM vs Static comparison
    print("\n## 8. LLM vs Static comparison")
    comp_rows = []
    for vt in VIOLATION_TYPES:
        llm_col = df[f"{vt}_llm"]
        static_col = df[f"{vt}_static"]
        try:
            sl, ss = llm_col.std(), static_col.std()
            if pd.isna(sl) or pd.isna(ss) or sl == 0 or ss == 0:
                corr = 0
            else:
                r, _ = pearsonr(llm_col, static_col)
                corr = r if not (r != r) else 0
        except Exception:
            corr = 0
        static_nonzero = static_col > 0
        if static_nonzero.any():
            ratio = llm_col[static_nonzero].mean() / static_col[static_nonzero].mean()
        else:
            ratio = 0
        flag = ""
        if ratio > 3:
            flag = " (LLM >> Static)"
        elif ratio > 0 and ratio < 0.33:
            flag = " (Static >> LLM)"
        comp_rows.append({"type": vt, "correlation": corr, "mean_ratio": ratio, "flag": flag})
    comp_df = pd.DataFrame(comp_rows)
    print(comp_df.to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze v3 evaluation results")
    parser.add_argument("--input", "-i", type=str, default="evaluations_v3-*.jsonl", help="Input glob or path")
    parser.add_argument("--export", "-e", type=str, default=None, help="Export to CSV (e.g. insights_v3.csv)")
    parser.add_argument("--year", "-y", type=str, default=None, help="Filter by year(s), e.g. 2023 or 2023,2024")
    parser.add_argument("--semester", "-s", type=str, default=None, help="Filter by semester substring (e.g. Spring 2025)")
    args = parser.parse_args()

    records = load_evaluations(args.input)
    if not records:
        print("No evaluation files found (evaluations_v3-*.jsonl)", file=sys.stderr)
        sys.exit(1)

    df = build_dataframe(records)

    # Post-load fix: reclassify repo_name "reimplementation-back-end" (hyphenated) as backend
    backend_mask = df["repo_name"].astype(str).str.lower().str.contains("reimplementation-back-end", na=False)
    df.loc[backend_mask, "project_type"] = "reimplementation_backend"
    df.loc[backend_mask, "project_category"] = "reimplementation"

    year_filter = None
    if args.year:
        year_filter = [int(y.strip()) for y in args.year.split(",")]

    df = filter_records(df, year_filter=year_filter, semester_filter=args.semester)

    run_analysis(df, records)

    if args.export and len(df) > 0:
        out = Path(args.export)
        export_cols = [
            "project_id", "semester", "sort_key", "season", "year",
            "repo_name", "project_type", "project_category",
            "total_llm_violations", "total_static_violations", "alignment_score",
        ]
        for vt in VIOLATION_TYPES:
            export_cols.append(f"{vt}_llm")
        for vt in VIOLATION_TYPES:
            export_cols.append(f"{vt}_static")
        df[export_cols].to_csv(out, index=False)
        print(f"\nExported to {out}")


if __name__ == "__main__":
    main()
