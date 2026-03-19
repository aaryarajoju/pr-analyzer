# PR-Analyzer System Architecture

Structured summary for the system architecture section of the thesis paper. Exhaustive coverage of project structure, pipeline stages, data formats, models & prompts, static analyzers, dependencies, and known limitations.

---

## 1. Project Structure

```
pr-analyzer/
├── extract_data_v2.py          # Stage 1: Data extraction (GitHub API or cache)
├── evaluate_design_v3.py       # Stage 2: Main evaluation pipeline (static + LLM)
├── analyze_evaluations_v3.py   # Stage 3: Post-hoc analysis & insights
├── build_exemplar_index.py     # Exemplar index builder (TF-IDF)
├── run_evaluation.sh           # Batch runner for all years
├── design_insights.ipynb       # Jupyter notebook for visualizations
├── requirements.txt            # Python dependencies
├── projects.csv                # Input: project metadata (not in repo)
├── pr_cache/                   # Local cache: diff.txt, wiki.txt, files/, meta.json per project
├── dataset-YYYY.jsonl          # Extracted dataset per year
├── evaluations_v3-YYYY.jsonl   # Evaluation output per year
├── insights_v3.csv             # Aggregated insights export
├── exemplar_index.json         # TF-IDF index for exemplar retrieval
├── static_analyzer/            # Python static analysis orchestration
│   ├── run_all.py              # Entry point: partitions files, runs Ruby/TS, merges
│   ├── ruby_static_analyzer.py # Wrapper for parsers/static_analyzer.rb
│   ├── ts_static_analyzer.py   # LoD, long_chain, CMO for TypeScript
│   ├── *_detector.py           # Extractors + summarizers per violation type
│   └── ...
└── parsers/
    ├── static_analyzer.rb      # Ruby: Prism AST, 13 violation detectors
    ├── ruby_parser.rb          # Structure-only Ruby parser
    ├── ts_parser.ts            # TypeScript structure (hooks, components)
    ├── controller.py           # CLI for parsers (--static mode)
    ├── Gemfile                 # Ruby: prism
    └── package.json            # Node: tsx for ts_parser.ts
```

---

## 2. Pipeline Stages

### Stage 1: Data Extraction (`extract_data_v2.py`)

**Input:** `projects.csv` with columns: Project ID, links, Feedback on project, Feedback on design doc.

**Modes:**
- **Live:** Fetches from GitHub API (PR diffs, file contents) and Expertiza Wiki. Requires `GITHUB_TOKEN` (5000 req/hr vs 60 unauthenticated).
- **From-cache:** Builds entirely from `pr_cache/<project_id>/` (diff.txt, wiki.txt, files/, meta.json). No network.

**Output:** JSONL records per project. Each record: `project_id`, `semester`, `season`, `year`, `pr_urls`, `repo_name`, `diff`, `wiki_content`, `project_feedback`, `design_feedback`, `full_files` (list of `{path, content}`).

**Key logic:**
- Parses GitHub PR URLs and Wiki URLs from `links` column.
- Extracts `repo_name` from first PR URL (e.g. `expertiza`, `expertiza-reimplementation-backend`).
- Fetches full file content only for `.rb`, `.ts`, `.tsx` (static analysis targets).
- Max 500 KB per file. Fork PRs use `head_owner`/`head_repo` for Contents API.

### Stage 2: Design Evaluation (`evaluate_design_v3.py`)

**Input:** `dataset-YYYY.jsonl`, `pr_cache/`, `exemplar_index.json`.

**Per-project flow:**
1. **Parse diff** → extract `changed_files` (from `diff --git a/X b/Y`), filter out `db/migrate/`, `spec/`, `test/`.
2. **Load cached files** → only files in `changed_files` (or from record `full_files`).
3. **Static analysis** → run on diff-touched files only; exclude migrations, spec, test.
4. **Build diff-anchored context** → filtered diff + full method bodies for touched symbols; cap 20K chars; exclude test/spec files.
5. **Exemplar retrieval** → TF-IDF similarity over diff + feedback; top 1 exemplar (min similarity 0.1).
6. **LLM batch calls** → 3 sequential batches (A: structural, B: method-level, C: coupling).
7. **Alignment call** → compare violation summary vs instructor feedback; score 1–5.
8. **Write** → append one JSON line to output JSONL.

**Output:** `evaluations_v3-YYYY.jsonl`. Each record: `project_id`, `semester`, `repo_name`, `project_type`, `project_category`, `files_in_pr`, `files_statically_analyzed`, `static_findings`, `llm_findings`, `violation_counts`, `static_counts`, `alignment_score`, `alignment_explanation`, `feedback_mentions_violations`, `exemplars_used`, `llm_failed`.

**Optimizations:**
- Skips projects already in output file (resumable).
- `--no-llm`: static analysis only.
- `--fast`: skip alignment and exemplars.
- `--concurrency N`: parallel project processing (default 3).

### Stage 3: Analysis & Export (`analyze_evaluations_v3.py`)

**Input:** `evaluations_v3-*.jsonl` (glob).

**Output:** Console analysis (overview, per-type breakdown, by project category, semester trends, top violators, co-occurrence, severity, LLM vs static comparison) and optional `insights_v3.csv` export.

**Post-load fix:** Reclassifies `repo_name == "reimplementation-back-end"` as `project_type=reimplementation_backend`, `project_category=reimplementation`.

---

## 3. Data Formats

### projects.csv

| Column | Description |
|--------|-------------|
| Project ID | Unique ID (e.g. E2541, 2007_1) |
| links | GitHub PR URLs, Wiki URLs (space/comma separated) |
| Feedback on project | Instructor feedback text |
| Feedback on design doc | Design review feedback |

First column may be semester (e.g. "Spring 2025 Final"); merged cells are forward-filled.

### pr_cache layout

```
pr_cache/<project_id>/
├── diff.txt       # Raw PR diff(s), concatenated
├── wiki.txt       # Wiki page content (HTML stripped)
├── meta.json      # project_feedback, design_feedback (overrides CSV)
└── files/         # Full file content; names: <sha>_<path_with__>.rb
```

### dataset-YYYY.jsonl (extraction output)

```json
{
  "project_id": "E2541",
  "semester": "Spring 2025",
  "season": "Spring",
  "year": 2025,
  "pr_urls": ["https://github.com/owner/repo/pull/123"],
  "repo_name": "expertiza-reimplementation-backend",
  "diff": "...",
  "wiki_content": "...",
  "project_feedback": "...",
  "design_feedback": "...",
  "full_files": [{"path": "app/controllers/x.rb", "content": "..."}]
}
```

### evaluations_v3-YYYY.jsonl (evaluation output)

```json
{
  "project_id": "E2541",
  "semester": "Spring 2025",
  "repo_name": "expertiza-reimplementation-backend",
  "project_type": "reimplementation_backend",
  "project_category": "reimplementation",
  "files_in_pr": 5,
  "files_statically_analyzed": 3,
  "static_findings": {"srp": {...}, "lod": {...}, ...},
  "llm_findings": {"srp": {"violations": [...], "count": N}, ...},
  "violation_counts": {"srp": 2, "dry": 1, ..., "total": 15},
  "static_counts": {"srp": 1, ..., "total": 8},
  "alignment_score": 3,
  "alignment_explanation": "...",
  "feedback_mentions_violations": false,
  "exemplars_used": ["E2401"],
  "llm_failed": false
}
```

### exemplar_index.json

```json
{
  "exemplars": [{"project_id": "...", "project_feedback": "...", "violation_hints": [...], "embedding": [...]}],
  "vocabulary": ["word1", "word2", ...],
  "idf": [0.5, 1.2, ...]
}
```

---

## 4. Models & Prompts

### LLM Backend

- **Service:** Ollama (local), `http://localhost:11434/api/generate`
- **Default model:** `deepseek-coder-v2:16b-lite-instruct-q4_K_M`
- **Format:** `stream: false`, `format: "json"`
- **Timeout:** 120s per batch, 60s for alignment

### Batch A (Structural): SRP, God Object, CMO, LSP, OCP

- **Diff-focus:** "Focus ONLY on code added or modified in the diff (lines starting with +)."
- **Static pre-scan:** Injected as `## Static analysis pre-scan\n{static_summary}`.
- **Exemplar context:** Optional reference example (project_id, feedback, violation hints).
- **Output schema:** JSON with `violations` (max 3 listed) and `count` (full total) per type.
- **Definitions:** Rich multi-line definitions for each violation type.

### Batch B (Method-level): Feature Envy, Long Method, DRY, Information Expert, DIP

- **Diff-focus:** "For method-level violations, consider BOTH (a) methods ADDED in the diff, (b) methods in files touched by the diff that static analysis flagged."
- **Static pre-scan:** Detailed — top 3 violations per type with method names and metrics.
- **Output schema:** Same structure; method-level fields (class, method, reason, severity).

### Batch C (Coupling): LoD, Long Chain, Shotgun Surgery

- **Diff-focus:** Same as Batch A (strict diff-only).
- **Static pre-scan:** Summary strings.
- **Output schema:** LoD/long_chain (location, chain, reason); shotgun_surgery (concern, files_affected).

### Alignment Prompt

- Compares instructor feedback (truncated to 500 chars) with violation summary.
- Scoring: 5 = explicit match, 4 = general consistency, 3 = neutral (default for positive feedback), 2 = contradiction, 1 = explicit contradiction.
- Returns `alignment_score`, `explanation`, `feedback_mentions_violations`.

### Placeholder Handling

- After each batch, checks for placeholder values (`class="X"`, `location="X"`, `chain="Y"`).
- If found: zero out counts/violations for that type, log warning.

---

## 5. Static Analyzers

### Ruby (parsers/static_analyzer.rb)

**Parser:** Prism AST. **Language:** Ruby only.

**13 violation types with thresholds:**

| Type | Thresholds | Notes |
|------|------------|-------|
| SRP | 7 methods (12 for controllers), 5 instantiations | Controllers use higher method limit |
| God Object | 15+ methods, 10+ ivars, 8+ external instantiations | |
| CMO | 50%+ class methods, min 3 class methods | |
| LSP | Arity mismatch in overrides | Parent/child method arity |
| OCP | 4+ branches, 2+ type checks; or update_/handle_/process_ + 3 elsif | |
| LoD | Depth ≥ 3, root foreign (not @ivar, param, block param, Rails/ENV/params) | Excludes Rails roots |
| Long Chain | Depth ≥ 5 | Same exclusions as LoD |
| DRY | Structural SHA1 hash; min 30 chars body, 2+ duplicates | |
| Feature Envy | 6 external vs 2× own (general); 10 vs 3× (controllers) | Helpers, migrations excluded |
| Information Expert | 8 external vs 3× ivar (general); 12 vs 4× (controllers) | Same exclusions |
| Long Method | 20+ lines | |
| Shotgun Surgery | 8+ external class refs per file | Excludes comments/strings |
| DIP | 2+ direct `.new` calls (excl. interface-named) | |

**Exclusions:**
- `db/migrate/`, `spec/`, `test/` (by evaluate_design_v3, not in Ruby script).
- Helper classes, migration classes: excluded entirely from Feature Envy and Information Expert.

### TypeScript (static_analyzer/ts_static_analyzer.py)

**Parser:** `parsers/ts_parser.ts` (Node/tsx). **Languages:** `.ts`, `.tsx`.

**Supported:** LoD (depth ≥ 3, foreign root), Long Chain (depth ≥ 5), CMO (4+ hooks, no components).

**Not implemented:** SRP, DRY, LSP, God Object, Feature Envy, Long Method, Shotgun Surgery, OCP, DIP, Information Expert — deferred to LLM.

### Python Orchestration (static_analyzer/run_all.py)

- Partitions files by extension (`.rb` → Ruby, `.ts`/`.tsx` → TypeScript).
- Runs Ruby via subprocess (temp files, 60s timeout).
- Runs TS analyzer in-process (regex + ts_parser subprocess).
- Merges findings; adds `summaries` dict for LLM prompt construction.

---

## 6. Dependencies

### Python (requirements.txt)

```
aiohttp>=3.9.0
beautifulsoup4>=4.12.0
matplotlib>=3.7.0
pandas>=2.0.0
requests>=2.28.0
scikit-learn>=1.0.0
scipy>=1.10.0
tqdm>=4.66.0
```

**Implicit:** Python 3.9+.

### Ruby (parsers/Gemfile)

```
gem "prism"
```

**Runtime:** `bundle exec ruby static_analyzer.rb` (or plain `ruby` if prism in path).

### Node (parsers/package.json)

- `tsx` for running `ts_parser.ts`.

### External Services

- **Ollama:** Local LLM server. Must run `ollama serve` and pull model.
- **GitHub API:** For live extraction. `GITHUB_TOKEN` or `GH_TOKEN` for higher rate limits.

---

## 7. Current Known Limitations

### Data & Extraction

- **projects.csv required:** Not in repo; must be provided with correct columns.
- **GitHub rate limits:** 60/hr unauthenticated; 5000/hr with token. Fine-grained tokens need Contents + Pull requests read.
- **Fork PRs:** Uses head repo for file content; base repo for PR metadata.
- **File size:** 500 KB max per file; larger files skipped.
- **Languages:** Only Ruby, TypeScript/TSX for static analysis; other languages get LLM-only.

### Static Analysis

- **Ruby-only detectors:** SRP, DRY, LSP, God Object, Feature Envy, Long Method, Shotgun Surgery, OCP, DIP, Information Expert are Ruby-only. TypeScript gets LoD, Long Chain, CMO heuristics.
- **Threshold tuning:** Controller-specific thresholds (SRP, Feature Envy, Information Expert) are heuristic; may under/over-detect.
- **Information Expert metric:** Regex `@ivar` vs `a.b` may miss some patterns; controller thresholds (12, 4.0) can yield 0 on some codebases.
- **DRY:** Structural hash only; no semantic clone detection.
- **Shotgun Surgery:** File-level external ref count; does not trace change propagation.
- **Parse errors:** Silently return empty findings; no retry or partial output.

### LLM

- **Local only:** Ollama; no cloud API fallback.
- **JSON format:** Relies on `format: "json"`; malformed output can cause parse failure.
- **Placeholder detection:** Only checks `class="X"`, `location="X"`, `chain="Y"`; other placeholders may slip through.
- **Context cap:** 20K chars; large diffs truncated; method bodies prioritized by size.
- **Exemplar:** Single exemplar, min similarity 0.1; TF-IDF may not capture semantic similarity well.
- **Batch ordering:** Sequential (A→B→C); no parallelization of batches.
- **No retry:** LLM failure sets `llm_failed=true`, zeros all violations.

### Alignment

- **Subjective:** Score 1–5 depends on LLM interpretation.
- **Feedback truncation:** 500 chars; long feedback may lose critical context.
- **Default 3:** Positive feedback with no code-quality mention defaults to 3.

### Analysis & Visualization

- **Semester normalization:** Multiple formats supported; edge cases (e.g. "Final", "OSS") stripped; some formats may not parse.
- **sort_key:** `analyze_evaluations_v3.py` uses Fall=1, Spring=2 (Fall before Spring); `design_insights.ipynb` uses Spring=1, Fall=2 (Spring before Fall, per academic calendar). Inconsistent between scripts; notebook reflects correct chronological order.
- **Project type detection:** Heuristic from `repo_name`; hyphenated variants (`reimplementation-back-end`) require post-load fix in analyzer.

### Operational

- **Resumability:** Skips by project_id; re-running with same output appends only new projects; no idempotent overwrite.
- **Concurrency:** ThreadPoolExecutor for projects; each project runs batches sequentially; no GPU utilization for Ollama (handled by Ollama itself).
- **Cache layout:** Assumes `pr_cache/<project_id>/files/` with `<sha>_<path>__` naming; `extract_data_v2` from-cache expects this structure.
- **No incremental exemplar index:** Rebuild overwrites entire index.
