#!/usr/bin/env python3
"""
Analyze evaluation results across years.

Usage:
    python analyze_evaluations.py                    # all years
    python analyze_evaluations.py --year 2024       # single year
    python analyze_evaluations.py --year 2024,2025 # multiple years
    python analyze_evaluations.py --export csv       # export to CSV
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd


def load_evaluations(glob_pattern: str = "evaluations_hybrid-*.jsonl") -> list[dict]:
    """Load all evaluation files matching pattern."""
    records = []
    for p in sorted(Path(".").glob(glob_pattern)):
        year = p.stem.replace("evaluations_hybrid-", "")
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    r["year"] = year
                    records.append(r)
                except json.JSONDecodeError:
                    pass
    return records


def _static_total(r: dict) -> int:
    sf = r.get("static_findings", {})
    return sum(sf.get(k, {}).get("count", 0) for k in ("lod", "cmo", "srp", "dry", "lsp"))


def run_analysis(records: list[dict], year_filter: Optional[list[str]] = None) -> Optional[pd.DataFrame]:
    if year_filter:
        records = [r for r in records if r.get("year") in year_filter]

    if not records:
        print("No records to analyze.")
        return None

    df = pd.DataFrame([
        {
            "project_id": r["project_id"],
            "year": r["year"],
            "total_violations": r.get("total_violations", 0),
            "SRP": r.get("violations", {}).get("SRP", 0),
            "DRY": r.get("violations", {}).get("DRY", 0),
            "LoD": r.get("violations", {}).get("LoD", 0),
            "CMO": r.get("violations", {}).get("ClassMethodOveruse", 0),
            "LSP": r.get("violations", {}).get("LSP", 0),
            "static_total": _static_total(r),
            "confidence": r.get("confidence", 0),
            "summary": r.get("summary", "")[:200],
        }
        for r in records
    ])

    print("=" * 60)
    print("INSIGHTS: Design Violation Analysis")
    print("=" * 60)

    # 1. By year
    print("\n## 1. Violations by year")
    by_year = df.groupby("year").agg({
        "project_id": "count",
        "total_violations": ["sum", "mean", "max"],
    }).round(1)
    by_year.columns = ["projects", "total_sum", "avg_per_project", "max"]
    print(by_year.to_string())

    # 2. Top violators
    print("\n## 2. Top 10 projects by total violations")
    top = df.nlargest(10, "total_violations")[["project_id", "year", "total_violations", "SRP", "DRY", "LoD", "CMO", "LSP"]]
    print(top.to_string(index=False))

    # 3. Violation type distribution
    print("\n## 3. Violation type distribution (all)")
    type_totals = df[["SRP", "DRY", "LoD", "CMO", "LSP"]].sum()
    for k, v in type_totals.sort_values(ascending=False).items():
        pct = 100 * v / type_totals.sum() if type_totals.sum() else 0
        print(f"  {k}: {v} ({pct:.1f}%)")

    # 4. LLM vs Static (where static available)
    with_static = df[df["static_total"] > 0].copy()
    if len(with_static) > 0:
        with_static["ratio"] = with_static["total_violations"] / with_static["static_total"]
        print("\n## 4. LLM vs Static analysis (projects with static)")
        print(f"  Projects: {len(with_static)}")
        print(f"  Avg LLM/static ratio: {with_static['ratio'].mean():.2f}")
        print(f"  Correlation: {with_static['total_violations'].corr(with_static['static_total']):.2f}")

    # 5. Zero-violation projects
    zero = df[df["total_violations"] == 0]
    print(f"\n## 5. Projects with 0 violations: {len(zero)} ({100*len(zero)/len(df):.1f}%)")
    if len(zero) <= 10:
        print(zero[["project_id", "year"]].to_string(index=False))
    else:
        print(zero[["project_id", "year"]].head(10).to_string(index=False))
        print(f"  ... and {len(zero)-10} more")

    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze evaluation results")
    parser.add_argument("--year", "-y", type=str, default=None, help="Filter by year(s), e.g. 2024 or 2024,2025")
    parser.add_argument("--export", "-e", type=str, default=None, help="Export to CSV: --export output.csv")
    args = parser.parse_args()

    records = load_evaluations()
    if not records:
        print("No evaluation files found (evaluations_hybrid-*.jsonl)")
        sys.exit(1)

    year_filter = None
    if args.year:
        year_filter = [y.strip() for y in args.year.split(",")]

    df = run_analysis(records, year_filter)

    if args.export and df is not None:
        out = Path(args.export)
        df.to_csv(out, index=False)
        print(f"\nExported to {out}")


if __name__ == "__main__":
    main()
