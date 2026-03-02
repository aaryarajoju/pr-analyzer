# PR Analyzer v2: Hybrid Static + LLM Design

**Purpose:** This document describes the new design for the PR Analyzer. Use it as a spec to implement the hybrid system. Do not modify existing code until this design is implemented.

---

## 1. Current State (Baseline)

- **extract_data.py:** Fetches PR diffs and Wiki content from GitHub API. Output: `dataset_raw.jsonl` with `project_id`, `diff`, `wiki_content`, `project_feedback`, `design_feedback`.
- **Parsers (parsers/):** Ruby (Prism) and TypeScript (ts) AST parsers extract structure only (classes, methods, external calls, components, hooks, imports). They do **not** detect violations.
- **evaluate_design.py / analyze_violations.py:** Send diff + optional parser output to LLM. **All violation detection is LLM-only.** No heuristics, no static rules.
- **Limitation:** Diffs contain fragments; parsers often fail. LLM has no grounded static findings.

---

## 2. New Design: Hybrid Static + LLM

**Goal:** Run static analysis first (deterministic, AST-based), then send findings + code to LLM for validation and refinement.

**Principles evaluated:** SRP, DRY, LoD (Law of Demeter), LSP (Liskov Substitution Principle), Class Method Overuse (CMO).

---

## 3. Full Code Instead of Diffs

**Decision:** Analyze **full file content** for changed paths, not just diff fragments.

**Why:**
- Parsers need complete, parseable code.
- Clone detection needs full methods/blocks.
- LoD chain detection needs full call expressions.
- Reduces parse failures and improves static analysis quality.

**Implication:** Extraction must clone repos and read full files for changed paths.

---

## 4. Extraction Changes

### 4.1 New Data Flow

```
projects.csv (Project ID, links to GitHub PRs, Wiki)
    │
    ▼
For each PR:
  1. Parse GitHub PR URL → owner, repo, pr_number
  2. Fetch PR metadata (GitHub API): head_sha, base_sha, files_changed[]
  3. For each changed file path: fetch file content at head_sha (GitHub API: contents)
  4. Fetch diff (existing) and Wiki content (existing)
    │
    ▼
Output: dataset.jsonl (or dataset_v2.jsonl) with:
  - project_id
  - diff (existing)
  - wiki_content (existing)
  - project_feedback, design_feedback (existing)
  - full_files: [{ path: string, content: string }]  # NEW
```

**Note:** GitHub API `GET /repos/{owner}/{repo}/contents/{path}?ref={sha}` returns file content. No need to clone repo if we only need changed files. Cloning is optional for local clone detection tools that expect a repo.

### 4.2 Implementation Options

**Option A (API only):** Use GitHub API to fetch each changed file's content at `head_sha`. No repo clone. Simpler.

**Option B (Clone):** Clone repo, checkout PR branch, read files from disk. Needed if we use tools like `flay` (Ruby) that expect a project directory.

**Recommendation:** Start with Option A. If clone detection tools require a repo, add Option B later.

---

## 5. Static Analysis

### 5.1 LoD (Law of Demeter) – Chain Detection

**Rule:** Flag method-call chains longer than N levels (e.g., 3). Example: `a.b.c.d` = 3 dots = violation.

**Implementation:**
- **Ruby:** Extend `ruby_parser.rb` (or new visitor). Walk `CallNode` chains. For `obj.m1.m2.m3`, count receiver chain depth.
- **TypeScript:** Walk `ts.CallExpression` chains. `expr.propertyName` or `expr.expression` for chained calls.

**Output:** `{ lod_violations: [{ file, line, chain: string, depth: number }], lod_count: number }`

### 5.2 CMO (Class Method Overuse)

**Rule:** Flag classes with high ratio of class methods vs instance methods.

**Implementation:**
- **Ruby:** Extend existing parser. `visit_def_node`: if `node.receiver` → class method; else → instance method. Per class: `class_methods / (class_methods + instance_methods)`. Flag if ratio > threshold (e.g., 0.5).
- **TypeScript:** Less common for class methods; focus on Ruby. TS could use static methods vs instance methods if needed.

**Output:** `{ cmo_violations: [{ file, class_name, ratio, class_method_count, instance_method_count }], cmo_count: number }`

### 5.3 DRY – Code Clone Detection

**Rule:** Detect duplicated or near-duplicate code blocks.

**Implementation options:**

| Option | Ruby | TypeScript | Notes |
|--------|------|------------|-------|
| **flay** | `flay` gem | N/A | AST-based, Ruby only |
| **jscpd** | N/A | `jscpd` (npm) | Supports TS, configurable |
| **Custom** | Prism AST → hash subtrees | ts AST → hash subtrees | Full control |

**Clone types:** Type 1 (exact), Type 2 (renamed identifiers), Type 3 (small edits).

**Output:** `{ dry_violations: [{ file1, line1, file2, line2, similarity }], dry_count: number }`

**Recommendation:** Use `flay` for Ruby, `jscpd` for TypeScript. Parse their output into a structured format.

### 5.4 SRP – Heuristics

**Rule:** Coarse signals only. No strict detection.

**Heuristics:**
- Method count per class (e.g., > 20 = suspicious)
- Number of distinct external types called (high coupling)
- Number of responsibilities inferred from method names (optional, harder)

**Output:** `{ srp_signals: [{ file, class_name, method_count, external_count }], srp_count: number }`

### 5.5 LSP (Liskov Substitution Principle)

**Definition:** Subtypes must be substitutable for their base types without breaking correctness. If S is a subtype of T, any program using T should work with S.

**Challenge:** LSP is semantic and behavioral. Full static detection requires contracts, pre/postconditions, or formal specs. Without those, we use **structural signals** and rely heavily on the LLM for interpretation.

**Possible static signals (heuristics):**

| Signal | Description | Implementation |
|--------|-------------|----------------|
| **Override with fewer params** | Subclass method has fewer parameters than parent | Compare method signatures in inheritance hierarchy |
| **Override with different return type** | Subclass returns incompatible type | Ruby: hard (duck typing). TS: type checker can detect |
| **New exceptions in override** | Subclass method raises exceptions parent didn't | Scan for `raise`/`throw` in overridden methods |
| **Broken inheritance chain** | Subclass doesn't call `super` where parent has logic | Detect overrides that omit `super` in critical methods |
| **Subclass narrows accepted types** | Override adds stricter type checks (e.g., `raise unless x.is_a?(SpecificType)`) | Pattern match for type guards in overrides |

**Implementation:**
- **Ruby:** Build inheritance graph from `class Child < Parent`. For each override (`def method` in subclass where parent defines it), compare arity, check for new `raise`, check for missing `super`.
- **TypeScript:** Use `ts` API to find class extends, method overrides. Type checker can flag return-type mismatches if `strict` is on.

**Output:** `{ lsp_signals: [{ file, class_name, parent_class, method, signal_type: string }], lsp_count: number }`

**Note:** LSP violations are often subtle. Static analysis yields *candidates*; the LLM should validate and explain. Consider LSP as **LLM-primary** with static hints, unlike LoD/DRY which are more deterministic.

### 5.6 Static Analysis Output Schema

```json
{
  "lod": { "violations": [...], "count": 5 },
  "cmo": { "violations": [...], "count": 2 },
  "dry": { "violations": [...], "count": 8 },
  "srp": { "signals": [...], "count": 3 },
  "lsp": { "signals": [...], "count": 2 }
}
```

---

## 6. LLM Hybrid Flow

**Input to LLM:**
1. Full file content (or diff, if token limits are a concern)
2. **Static analysis findings** (the new schema above)
3. Wiki content

**Prompt structure:**
```
Static analysis found:
- LoD: 5 chain violations (depth 3+)
- CMO: 2 classes with high class-method ratio
- DRY: 8 clone pairs
- SRP: 3 classes with 20+ methods
- LSP: 2 override signals (e.g., new exceptions, missing super)

[Code / Diff]

Validate these findings. Refine counts if needed. Add any violations the static analysis missed. For LSP, check whether subtypes can substitute for base types without breaking callers. Return JSON with:
{ violations: { SRP, DRY, LoD, LSP, ClassMethodOveruse }, total_violations, confidence, summary }
```

**Output:** Same as current `analyze_violations.py` – structured JSON with counts and summary.

---

## 7. New Pipeline Architecture

```
dataset_raw.jsonl (existing) OR dataset_v2.jsonl (with full_files)
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  STATIC ANALYZER (new module)                            │
│  - Input: full_files[] per project                       │
│  - LoD: chain depth (Ruby + TS parsers)                  │
│  - CMO: class/instance method ratio (Ruby parser)        │
│  - DRY: flay (Ruby) + jscpd (TS)                         │
│  - SRP: heuristics (method count, coupling)              │
│  - LSP: override signals (exceptions, super, signatures)│
│  - Output: static_findings.json per project              │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  HYBRID EVALUATOR (new or updated script)                │
│  - Input: full_files, diff, wiki, static_findings         │
│  - Build prompt: static findings + code                   │
│  - Call LLM (Ollama)                                     │
│  - Output: evaluations.jsonl (or violations_report)      │
└─────────────────────────────────────────────────────────┘
```

---

## 8. File Structure (Proposed)

```
pr-analyzer/
├── extract_data.py          # existing
├── extract_data_v2.py       # NEW: fetch full files for changed paths
├── static_analyzer/          # NEW module
│   ├── __init__.py
│   ├── lod_detector.py      # chain depth
│   ├── cmo_detector.py      # class method ratio
│   ├── dry_detector.py      # clone detection (flay, jscpd)
│   ├── srp_detector.py      # heuristics
│   ├── lsp_detector.py      # override signals (Liskov)
│   └── run_all.py          # run all, output combined JSON
├── evaluate_design_hybrid.py # NEW: static + LLM
├── analyze_violations.py    # existing (can keep for diff-only mode)
├── parsers/                 # existing; extend for LoD/CMO
├── dataset_raw.jsonl        # existing
├── dataset_v2.jsonl         # NEW: with full_files
└── new-design.md            # this file
```

---

## 9. Implementation Order

1. **extract_data_v2.py** – Fetch full file content for changed paths via GitHub API.
2. **extend parsers** – Add LoD chain visitor, CMO ratio calculation.
3. **static_analyzer/lod_detector.py** – Implement chain-depth detection.
4. **static_analyzer/cmo_detector.py** – Implement class-method ratio.
5. **static_analyzer/dry_detector.py** – Integrate flay (Ruby), jscpd (TypeScript).
6. **static_analyzer/srp_detector.py** – Implement heuristics.
7. **static_analyzer/lsp_detector.py** – Implement override-signal detection (inheritance, super, exceptions).
8. **evaluate_design_hybrid.py** – Combine static + LLM, new prompt format.
9. **Optional:** Update analyze_violations to support hybrid mode.

---

## 10. Open Decisions

- **Clone vs API:** Use GitHub API for file content (Option A) first. Add repo clone if tools require it.
- **LLM input size:** Full files vs diff + static findings only. Consider token limits; may need to truncate or summarize.
- **Clone detection tools:** Confirm flay (Ruby) and jscpd (TypeScript) compatibility. May need custom output parsing.
- **LSP scope:** Start with override signals (exceptions, super, arity). Expand to type-checker integration (TS) if feasible.
- **Backward compatibility:** Keep existing extract_data and analyze_violations for diff-only mode.

---

## 11. Summary

- **Full code:** Fetch full file content for changed paths (GitHub API).
- **Static analysis:** LoD (chains), CMO (ratio), DRY (clones), SRP (heuristics), LSP (override signals).
- **LLM:** Receives static findings + code, validates and refines.
- **Output:** Same CSV/MD format as current analyze_violations, but with hybrid grounding.
