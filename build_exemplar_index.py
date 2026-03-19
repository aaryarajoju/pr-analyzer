#!/usr/bin/env python3
"""
build_exemplar_index.py – Build a searchable index of past PRs using instructor feedback.

Uses TF-IDF embeddings (no external API) for few-shot exemplar retrieval.
Given a new PR, find_similar_exemplars() returns the most similar past PRs with
their instructor feedback and violation hints.

Re-running overwrites exemplar_index.json completely (rebuild from scratch).
Embeddings are refit on the full corpus each time.

Usage:
    python build_exemplar_index.py --input projects.csv --cache pr_cache/ --output exemplar_index.json

Dependencies: stdlib + scikit-learn (TfidfVectorizer)
"""

import argparse
import json
import math
import re
from pathlib import Path
from typing import List, Optional, Tuple, Union

import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Violation hints extraction (all patterns use \b word boundaries)
# ─────────────────────────────────────────────────────────────────────────────
def _kw(*phrases: str) -> list:
    """Convert keyword phrases to regex patterns with \\b word boundaries."""
    out = []
    for p in phrases:
        words = p.split()
        out.append(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b")
    return out


VIOLATION_PATTERNS = [
    ("SRP", _kw(
        "single responsibility", "SRP", "too many responsibilities", "does too much",
        "multiple concerns", "mixed concerns", "too many methods", "god class", "large class",
        "too much logic", "should be split", "separate the", "violates separation",
        "handles too many", "too complex", "bloated", "monolithic", "refactor into",
        "break this into", "extract", "not cohesive", "low cohesion",
    )),
    ("DRY", _kw(
        "DRY", "don't repeat", "duplicate", "duplication", "repetition", "repeated logic",
        "copy", "copied", "redundant", "reuse", "code reuse", "helper method",
        "abstract", "consolidate", "similar logic", "same logic", "refactor", "shared",
    )),
    ("LoD", _kw(
        "law of demeter", "LoD", "method chain", "chaining", "too many dots", "reaches into",
        "directly access", "tight coupling", "coupled", "coupling", "knows too much",
        "reaches through", "tell don't ask", "demeter", "navigation", "deep access",
    )),
    ("CMO", _kw(
        "class method", "static method", "class methods", "too many class methods",
        "instance method", r"self.", "use instance", "stateless", "utility class",
        "all static", "procedural",
    )),
    ("LSP", _kw(
        "LSP", "liskov", "override", "overrides", "subclass", "inheritance", "inherits",
        "parent class", "base class", "violates contract", "breaks contract", "polymorphism",
        "substitution", "extends", "super", "incompatible",
    )),
    ("God Object", _kw(
        "god object", "god class", "too large", "too big", "doing everything", "knows everything",
        "massive class", "huge class", "large class", "oversized", "too many fields",
        "too many attributes", "accumulates", "catch-all", "jack of all", "dumping ground",
    )),
    ("Feature Envy", _kw(
        "feature envy", "envious", "uses too much", "relies too heavily", "too dependent on",
        "borrows from", "reaches into", "accesses too many", "should belong to", "move this method",
        "method belongs", "wrong class", "misplaced method", "misplaced logic",
    )),
    ("Long Method", _kw(
        "long method", "too long", "method is too", "lengthy method", "large method",
        "method length", "break down", "break up this method", "split this method",
        "too many lines", "method does too much", "method handles", "complex method",
    )),
    ("Shotgun Surgery", _kw(
        "shotgun surgery", "too many files", "changes spread", "spread across", "affects many",
        "touches many", "ripple effect", "cascade", "scattered", "too many places",
        "multiple files changed", "change propagates", "widespread changes",
    )),
    ("OCP", _kw(
        "open closed", "OCP", "open/closed", "modifying existing", "should extend",
        "instead of modifying", "closed for modification", "open for extension",
        "adding conditions", "long if else", "long switch", "type checking", "isinstance",
        "hardcoded type", "should use inheritance", "use polymorphism instead",
    )),
    ("General", _kw(
        "poor design", "bad design", "design issue", "design problem", "needs improvement",
        "could be improved", "not well designed", "design could", "design is", "improvement needed",
        "design pattern", "restructure", "reorganize", "rethink", "concerns",
    )),
]


def extract_violation_hints(project_feedback: str, design_feedback: str) -> list[str]:
    """Scan feedback text for keyword signals; return list of violation type hints."""
    text = f"{project_feedback or ''} {design_feedback or ''}".lower()
    hints = []
    for label, patterns in VIOLATION_PATTERNS:
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                if label not in hints:
                    hints.append(label)
                break
    return hints


def parse_year_from_project_id(project_id: str) -> Optional[int]:
    """E2541 -> 2025, E2431 -> 2024. Project ID encodes year only, not season."""
    s = str(project_id).strip()
    if not s.upper().startswith("E") or len(s) < 4:
        return None
    try:
        yy = int(s[1:3])
        if 20 <= yy <= 99:
            return 2000 + yy
    except ValueError:
        pass
    return None


def parse_semester_from_csv_cell(cell: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Extract semester info from cell, e.g. 'Spring 2025 Final' or 'Fall 2024 Final'.
    Returns (semester, season, year) e.g. ("Spring 2025", "Spring", 2025).
    """
    if not cell:
        return None, None, None
    s = str(cell).strip()
    season = None
    if re.search(r"\bSpring\b", s, re.IGNORECASE):
        season = "Spring"
    elif re.search(r"\bFall\b", s, re.IGNORECASE):
        season = "Fall"
    m = re.search(r"\b(20\d{2})\b", s)
    year = int(m.group(1)) if m else None
    if season and year:
        semester = f"{season} {year}"
        return semester, season, year
    if year:
        return str(year), None, year
    return None, None, None


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────
def load_projects_csv(path: Path) -> list[dict]:
    """Load projects from CSV. Returns list of {project_id, project_feedback, design_feedback, semester, season, year}."""
    df = pd.read_csv(path, encoding="utf-8")
    # Forward-fill first column for merged cells (Google Sheets export)
    df.iloc[:, 0] = df.iloc[:, 0].replace("", pd.NA).ffill()

    def _str(v):
        return "" if pd.isna(v) else str(v).strip()

    records = []
    first_col = df.columns[0]
    for _, row in df.iterrows():
        pid = _str(row.get("Project ID") or row.get("project_id") or "")
        if not pid:
            continue
        pf = _str(row.get("Feedback on project") or "")
        design_fb = _str(row.get("Feedback on design doc") or "")
        semester, season, year = parse_semester_from_csv_cell(_str(row.get(first_col) or ""))
        if year is None:
            year = parse_year_from_project_id(pid)
            if year and not semester:
                semester = str(year)
        records.append({
            "project_id": pid,
            "project_feedback": pf,
            "design_feedback": design_fb,
            "semester": semester,
            "season": season,
            "year": year,
        })
    return records


def load_diff_from_cache(cache_dir: Path, project_id: str) -> str:
    """Read diff from pr_cache/<project_id>/diff.txt."""
    diff_path = cache_dir / project_id / "diff.txt"
    if diff_path.exists():
        return diff_path.read_text(encoding="utf-8", errors="replace")
    return ""


def load_diff_from_datasets(dataset_glob: str = "dataset-*.jsonl") -> dict:
    """Load diffs from dataset-YYYY.jsonl. Returns {project_id: (diff, year)}."""
    result = {}
    for p in sorted(Path(".").glob(dataset_glob)):
        year_str = p.stem.replace("dataset-", "")
        try:
            year = int(year_str)
        except ValueError:
            year = None
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    pid = obj.get("project_id", "")
                    if pid and pid not in result:
                        diff = obj.get("diff", "") or ""
                        result[pid] = (diff, year)
                except json.JSONDecodeError:
                    pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Index building
# ─────────────────────────────────────────────────────────────────────────────
def build_index(
    projects_csv: Path,
    cache_dir: Optional[Path],
    output_path: Path,
) -> dict:
    """Build exemplar index and write to JSON."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    import numpy as np

    projects = load_projects_csv(projects_csv)
    dataset_diffs = load_diff_from_datasets()
    cache_dir = (cache_dir or Path("pr_cache")) if cache_dir else Path("pr_cache")

    exemplars = []
    for p in projects:
        pf = p["project_feedback"]
        df = p["design_feedback"]
        if not pf and not df:
            continue

        diff = load_diff_from_cache(cache_dir, p["project_id"]) if cache_dir.exists() else ""
        if not diff and p["project_id"] in dataset_diffs:
            diff, ds_year = dataset_diffs[p["project_id"]]
            if p["year"] is None and ds_year is not None:
                p["year"] = ds_year
            if p["semester"] is None and ds_year is not None:
                p["semester"] = str(ds_year)

        diff_summary = (diff or "")[:500]

        hints = extract_violation_hints(pf, df)

        exemplars.append({
            "project_id": p["project_id"],
            "semester": p["semester"],
            "season": p["season"],
            "year": p["year"],
            "project_feedback": pf,
            "design_feedback": df,
            "violation_hints": hints,
            "diff_summary": diff_summary,
        })

    if not exemplars:
        output_path.write_text(json.dumps({"exemplars": [], "vocabulary": [], "idf": []}, indent=2))
        print("No exemplars to index (all skipped).")
        return {"exemplars": [], "vocabulary": [], "idf": []}

    # TF-IDF over combined text
    docs = [
        f"{e['project_feedback']} {e['design_feedback']} {e['diff_summary']}"
        for e in exemplars
    ]
    vectorizer = TfidfVectorizer(
        max_features=5000,
        stop_words="english",
        lowercase=True,
        token_pattern=r"(?u)\b\w+\b",
    )
    X = vectorizer.fit_transform(docs)

    # Normalize to unit length
    norms = np.linalg.norm(X.toarray(), axis=1, keepdims=True)
    norms[norms == 0] = 1
    X_norm = (X.toarray() / norms).tolist()

    for i, ex in enumerate(exemplars):
        ex["embedding"] = X_norm[i]

    vocabulary = vectorizer.get_feature_names_out().tolist()
    idf = vectorizer.idf_.tolist()

    out = {
        "exemplars": exemplars,
        "vocabulary": vocabulary,
        "idf": idf,
    }
    output_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────────────
def _tfidf_query(query_text: str, vocabulary: list[str], idf: list[float]) -> list[float]:
    """Compute TF-IDF vector for query using stored vocabulary and idf."""
    from collections import Counter
    import re as re_mod

    tokens = re_mod.findall(r"(?u)\b\w+\b", query_text.lower())
    vocab_set = {w: i for i, w in enumerate(vocabulary)}
    tf = Counter(t for t in tokens if t in vocab_set)
    vec = [0.0] * len(vocabulary)
    for w, idx in vocab_set.items():
        if w in tf:
            vec[idx] = tf[w] * idf[idx]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def find_similar_exemplars(
    query_text: str,
    index: Union[List[dict], dict],
    top_k: int = 3,
    exclude_id: Optional[str] = None,
) -> List[dict]:
    """
    Given a query string (e.g. a new PR's diff summary),
    return the top_k most similar exemplars by cosine similarity.
    Returns list of exemplar dicts (without the embedding field, with added 'similarity' float).

    index: either a list of exemplar dicts (each with 'embedding') or the full index dict
           with keys 'exemplars', 'vocabulary', 'idf'.
    exclude_id: if set, exclude exemplars with this project_id before ranking.
    """
    if isinstance(index, dict):
        exemplars = index.get("exemplars", [])
        vocabulary = index.get("vocabulary", [])
        idf = index.get("idf", [])
    else:
        exemplars = index
        vocabulary = []
        idf = []

    if not exemplars:
        return []

    if exclude_id:
        exemplars = [ex for ex in exemplars if ex.get("project_id") != exclude_id]

    if vocabulary and idf:
        q_vec = _tfidf_query(query_text, vocabulary, idf)
    else:
        # Fallback: no vocabulary stored, cannot embed query
        return []

    scored = []
    for ex in exemplars:
        emb = ex.get("embedding", [])
        if len(emb) != len(q_vec):
            continue
        sim = sum(a * b for a, b in zip(q_vec, emb))
        out = {k: v for k, v in ex.items() if k != "embedding"}
        out["similarity"] = round(sim, 4)
        scored.append(out)

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build searchable exemplar index from projects.csv and pr_cache or datasets.",
    )
    parser.add_argument("--input", "-i", type=Path, default=Path("projects.csv"))
    parser.add_argument("--cache", "-c", type=Path, default=Path("pr_cache"))
    parser.add_argument("--output", "-o", type=Path, default=Path("exemplar_index.json"))
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: Input not found: {args.input}")
        return

    out = build_index(args.input, args.cache, args.output)

    exemplars = out.get("exemplars", [])
    if not exemplars:
        return

    # Summary
    hint_counts = {}
    for ex in exemplars:
        for h in ex.get("violation_hints", []):
            hint_counts[h] = hint_counts.get(h, 0) + 1

    all_labels = [label for label, _ in VIOLATION_PATTERNS]
    print("\n" + "=" * 50)
    print("Summary")
    print("=" * 50)
    print(f"Total exemplars indexed: {len(exemplars)}")
    print("Violation hints coverage:")
    for label in all_labels:
        n = hint_counts.get(label, 0)
        pct = 100 * n / len(exemplars)
        print(f"  {label}: {n} ({pct:.1f}%)")
    none_count = sum(1 for e in exemplars if not e.get("violation_hints"))
    print(f"  (none): {none_count} ({100 * none_count / len(exemplars):.1f}%)")


if __name__ == "__main__":
    main()
