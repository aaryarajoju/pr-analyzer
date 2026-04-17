# PR Analyzer – Architecture Document

Exhaustive reference for external architects. No summarization.

---

## PROJECT STRUCTURE

| File / Folder | Description |
|---------------|-------------|
| **Root** | |
| `extract_data.py` | Legacy extractor: fetches PR diffs and Wiki from GitHub; outputs `dataset_raw.jsonl` (no full_files). |
| `extract_data_v2.py` | Main extractor: fetches PR diffs, Wiki, and full file content for .rb/.ts/.tsx; outputs dataset JSONL with `full_files`. |
| `evaluate_design.py` | Legacy evaluator: parses diffs for file paths, runs parsers, sends parser output + wiki + diff to Ollama; outputs `evaluations.jsonl`. |
| `evaluate_design_hybrid.py` | **Primary evaluator**: runs static analysis (reference) + chunked LLM analysis (primary); outputs `evaluations_hybrid*.jsonl`. |
| `analyze_violations.py` | Legacy analyzer: sends diff + wiki to Ollama for violation counts; outputs CSV/Markdown. |
| `analyze_evaluations.py` | Aggregates `evaluations_hybrid-*.jsonl`; prints insights and optionally exports CSV. |
| `design_insights.ipynb` | Jupyter notebook: loads `insights.csv`, produces charts (violations by year, type distribution, top violators, LLM vs static, etc.). |
| `requirements.txt` | Python dependencies: aiohttp, beautifulsoup4, matplotlib, pandas, requests, tqdm. |
| `projects.csv` | Input: Project ID, links (GitHub PRs, Wiki), Feedback on project, Feedback on design doc. |
| `README.md` | Project overview. |
| `NEW-DESIGN.md` | Design spec for hybrid static + LLM pipeline. |
| `OLD-DESIGN.md` | Legacy design notes. |
| `PR_REVIEW_AND_USAGE.md` | Usage and review notes. |
| **static_analyzer/** | |
| `static_analyzer/__init__.py` | Exports `run_static_analysis`. |
| `static_analyzer/run_all.py` | Orchestrates Ruby + TypeScript static analyzers; merges findings; adds human summaries for LLM. |
| `static_analyzer/ruby_static_analyzer.py` | Wraps `parsers/static_analyzer.rb`; writes content to temp files, invokes Ruby subprocess, parses JSON. |
| `static_analyzer/ts_static_analyzer.py` | LoD regex scan + CMO heuristic on TS/TSX; calls `parsers/ts_parser.ts` for hooks/components. |
| `static_analyzer/lod_detector.py` | Extracts LoD from findings; produces one-line summary. |
| `static_analyzer/cmo_detector.py` | Extracts CMO from findings; produces one-line summary. |
| `static_analyzer/srp_detector.py` | Extracts SRP from findings; produces one-line summary. |
| `static_analyzer/dry_detector.py` | Extracts DRY from findings; produces one-line summary. |
| `static_analyzer/lsp_detector.py` | Extracts LSP from findings; produces one-line summary. |
| **parsers/** | |
| `parsers/controller.py` | CLI: runs `ruby_parser.rb`, `ts_parser.ts`, or `static_analyzer.rb` on files. |
| `parsers/static_analyzer.rb` | Ruby static analyzer using Prism AST; detects SRP, OCP, LSP, DIP, ISP, LoD, DRY, Information Expert, Encapsulation, CMO. |
| `parsers/ruby_parser.rb` | Ruby parser using Prism: extracts classes, instance/class methods, external calls, LoD chain lengths, CMO ratio. |
| `parsers/ts_parser.ts` | TypeScript parser: extracts components, hooks, imports; used by ts_static_analyzer for CMO heuristic. |
| `parsers/package.json` | Node deps: typescript, tsx (dev). |
| `parsers/Gemfile` | Ruby deps: prism. |
| `parsers/Gemfile.lock` | Locked Ruby gem versions. |
| `parsers/README.md` | Parser usage. |
| **Data files** | |
| `dataset-2021.jsonl` … `dataset-2025.jsonl` | Per-year extraction output (project_id, diff, wiki_content, project_feedback, design_feedback, full_files). |
| `dataset_raw.jsonl` | Legacy: diff + wiki only. |
| `evaluations_hybrid-2021.jsonl` … `evaluations_hybrid-2025.jsonl` | Per-year evaluation output. |
| `evaluations.jsonl` | Legacy evaluation output. |
| `insights.csv` | Exported analysis (from `analyze_evaluations.py --export`). |

---

## PIPELINE STAGES

### Stage 1: Data Extraction

| Attribute | Value |
|-----------|-------|
| **Script** | `extract_data_v2.py` |
| **Input** | `projects.csv` (columns: Project ID, links, Feedback on project, Feedback on design doc) |
| **Output** | `dataset_v2.jsonl` or `dataset-YYYY.jsonl` (per-year) |
| **Process** | For each project: parse GitHub PR URLs and Wiki URLs → fetch PR diffs via GitHub API → fetch Wiki HTML → extract main content → for each PR: fetch metadata (head_sha, files) → for each changed .rb/.ts/.tsx file: fetch full content via Contents API → write one JSON object per line |
| **Options** | `--input`, `--output`, `--limit`, `--year` (e.g. 2025 or 2024,2025), `--concurrency`, `--delay`, `--validate-token` |

### Stage 2: Evaluation (Hybrid Static + LLM)

| Attribute | Value |
|-----------|-------|
| **Script** | `evaluate_design_hybrid.py` |
| **Input** | `dataset-YYYY.jsonl` or `dataset_v2.jsonl` (must have `full_files` for static; falls back to `diff` if empty) |
| **Output** | `evaluations_hybrid.jsonl` or `evaluations_hybrid-YYYY.jsonl` |
| **Process** | For each record: (1) run static analysis on `full_files` → (2) chunk code into batches (50K chars) → (3) for each batch: send LLM prompt with code → parse JSON response → sum violation counts → (4) write result line immediately |
| **Options** | `--input`, `--output`, `--model`, `--limit`, `--skip`, `--no-static`, `--max-chars` |

### Stage 3: Analysis & Export

| Attribute | Value |
|-----------|-------|
| **Script** | `analyze_evaluations.py` |
| **Input** | `evaluations_hybrid-*.jsonl` (glob) |
| **Output** | Console report + optional CSV (`--export insights.csv`) |
| **Process** | Load all matching JSONL files → add `year` from filename → filter by `--year` if given → compute by-year stats, top violators, type distribution, LLM vs static, zero-violation count → optionally export DataFrame to CSV |
| **Options** | `--year`, `--export` |

### Stage 4: Visualization

| Attribute | Value |
|-----------|-------|
| **Notebook** | `design_insights.ipynb` |
| **Input** | `insights.csv` (from Stage 3) |
| **Output** | Inline charts (violations by year, type distribution, top violators, stacked bars, LLM vs static scatter, zero-violation bars) |

---

## DATA FORMATS

### 1. Dataset JSONL (extract_data_v2.py output)

Each line is a JSON object:

```json
{
  "project_id": "E2541",
  "diff": "diff --git a/... (unified diff text)",
  "wiki_content": "extracted HTML text from Wiki",
  "project_feedback": "instructor feedback on project",
  "design_feedback": "instructor feedback on design doc",
  "full_files": [
    {"path": "app/controllers/foo.rb", "content": "full file content as string"},
    {"path": "src/components/Bar.tsx", "content": "..."}
  ]
}
```

- `full_files`: only .rb, .ts, .tsx; fetched via GitHub Contents API at head_sha; files > 500KB skipped.
- If no full_files (e.g. rate limit), evaluation falls back to `diff` only.

### 2. Evaluation JSONL (evaluate_design_hybrid.py output)

Each line:

```json
{
  "project_id": "E2541",
  "static_findings": {
    "lod": {"violations": [...], "count": N},
    "cmo": {"violations": [...], "count": N},
    "srp": {"signals": [...], "count": N},
    "dry": {"violations": [...], "count": N},
    "lsp": {"signals": [...], "count": N},
    "files_analyzed": N
  },
  "violations": {
    "SRP": N, "DRY": N, "LoD": N, "ClassMethodOveruse": N, "LSP": N
  },
  "total_violations": N,
  "confidence": 1-5,
  "summary": "1-2 sentence summary"
}
```

- `static_findings`: from static analyzers; `summaries` key stripped before output.
- `violations`: from LLM (primary); summed across code batches.

### 3. Static Analysis Findings Schema (run_all.py / static_analyzer.rb)

```json
{
  "lod": {"violations": [{"file": "...", "class_name": "...", "method_name": "...", "line": N, "chain_depth": N, "description": "..."}], "count": N},
  "cmo": {"violations": [{"file": "...", "class_name": "...", "class_method_count": N, "instance_method_count": N, "ratio": 0.5, "description": "..."}], "count": N},
  "srp": {"signals": [{"file": "...", "class_name": "...", "method_count": N, "external_count": N, "description": "..."}], "count": N},
  "dry": {"violations": [{"class_name": "...", "method_name": "...", "file": "...", "line": N, "duplicate_count": N, "description": "..."}], "count": N},
  "lsp": {"signals": [{"file": "...", "class_name": "...", "parent_class": "...", "method_name": "...", "child_arity": N, "parent_arity": N, "description": "..."}], "count": N},
  "files_analyzed": N,
  "parse_errors": [{"file": "...", "error": "..."}],
  "summaries": {"lod": "...", "cmo": "...", "srp": "...", "dry": "...", "lsp": "..."}
}
```

### 4. Ruby Parser Output (ruby_parser.rb)

```json
{
  "classes": ["ClassName"],
  "instance_methods": ["method_name"],
  "class_methods": ["self.method_name"],
  "external_calls": ["OtherClass.method"],
  "lod_chain_lengths": [3, 4],
  "per_class_stats": {"ClassName": {"instance_methods": 5, "class_methods": 2}}
}
```

### 5. TypeScript Parser Output (ts_parser.ts)

```json
{
  "components": ["ComponentName"],
  "hooks": ["useState", "useEffect"],
  "imports": [{"module": "react", "specifiers": ["useState", "useEffect"]}]
}
```

### 6. Insights CSV (analyze_evaluations.py --export)

Columns: `project_id`, `year`, `total_violations`, `SRP`, `DRY`, `LoD`, `CMO`, `LSP`, `static_total`, `confidence`, `summary`

---

## MODELS & PROMPTS

### Model

- **Default**: `deepseek-coder-v2:16b-lite-instruct-q4_K_M`
- **API**: Ollama `http://localhost:11434/api/generate`
- **Call**: `POST` with `{"model": "...", "prompt": "...", "stream": false, "format": "json"}`

### Primary Prompt (evaluate_design_hybrid.py) – ACTIVE

```
You are a strict code reviewer. Your job is to COUNT design principle violations in the code below.

## Code to analyze
{code}

## Design principles (count each occurrence)
- **LoD (Law of Demeter)**: Method chains > 4 levels (e.g. obj.a.b.c.d.e). Count each chain.
- **SRP**: Classes with > 7 methods or multiple unrelated responsibilities. Count each class.
- **DRY**: Duplicate/similar logic. Count each duplication.
- **ClassMethodOveruse**: Classes where > 50% of methods are class methods. Count each class.
- **LSP**: Override with different arity than parent. Count each override.

Be thorough. Count every violation you see. Do not be lenient.

Return ONLY valid JSON (no markdown):
{
  "violations": {"SRP": N, "DRY": N, "LoD": N, "ClassMethodOveruse": N, "LSP": N},
  "total_violations": N,
  "confidence": 1-5,
  "summary": "1-2 sentence summary of main issues"
}
```

- `{code}`: one batch of full file content (or diff if no full_files), up to 50K chars per batch.

### Legacy Prompts (not used in main pipeline)

**evaluate_design.py** (parser + wiki + diff):

```
You are evaluating code design principles. Analyze the following and provide a concise evaluation for each principle.

## Parser Output (extracted structure from the code)
{parser_json}

## Wiki / Design Documentation
{wiki_content}

## Code Diff
{diff}

---

Evaluate the code changes for these design principles:
1. **SRP (Single Responsibility Principle)** - Does each class/module have a single reason to change?
2. **DRY (Don't Repeat Yourself)** - Is there unnecessary duplication?
3. **LoD (Law of Demeter)** - Are there long chains of method calls on other objects?
4. **Class Method Overuse** - Are class methods used appropriately vs instance methods?

Provide a brief assessment for each principle (1-3 sentences) and an overall design quality score (1-5) with brief justification.
```

**analyze_violations.py** (diff + wiki):

```
Analyze this code diff for design principle violations.

## Wiki / Design Documentation
{wiki}

## Code Diff
{diff}

---

Return ONLY valid JSON (no markdown, no extra text) with this exact structure:
{
  "violations": {
    "SRP": <number of SRP violations, 0 if none>,
    "DRY": <number of DRY violations>,
    "LoD": <number of Law of Demeter violations>,
    "ClassMethodOveruse": <number of class method overuse violations>
  },
  "total_violations": <sum of all violations>,
  "confidence": <1-5, how confident you are in this analysis>,
  "summary": "<1-2 sentence summary of main issues>"
}

Evaluate: SRP (Single Responsibility), DRY (Don't Repeat Yourself), LoD (Law of Demeter), Class Method Overuse.
```

---

## STATIC ANALYZERS

| Rule | Language | Implementation | Threshold / Logic |
|------|----------|----------------|-------------------|
| **LoD** | Ruby | `static_analyzer.rb` (Prism AST) | `LOD_MAX_CHAIN = 3`; chain depth ≥ 3 |
| **LoD** | TypeScript/TSX | `ts_static_analyzer.py` (regex) | `LOD_MAX_CHAIN = 3`; regex `(?:[a-z_$][a-zA-Z0-9_$]*\.){2,}...`; depth ≥ 3 |
| **SRP** | Ruby | `static_analyzer.rb` | `SRP_MAX_METHODS = 7`, `SRP_MAX_INITS = 5`; method_count ≥ 7 OR external instantiations ≥ 5 |
| **SRP** | TypeScript | Not implemented (LLM only) | — |
| **DRY** | Ruby | `static_analyzer.rb` (structural hash) | `DRY_MIN_BODY_CHARS = 30`, `DRY_MIN_DUPLICATES = 2`; SHA1 of normalized body |
| **DRY** | TypeScript | Not implemented | — |
| **CMO** | Ruby | `static_analyzer.rb` | `CMO_MIN_CLASS_METHS = 3`, `CMO_RATIO_THRESHOLD = 0.5`; class_methods/total > 0.5 |
| **CMO** | TypeScript/TSX | `ts_static_analyzer.py` (heuristic) | hooks ≥ 4 and no components → violation |
| **LSP** | Ruby | `static_analyzer.rb` | Override with different arity than parent |
| **LSP** | TypeScript | Not implemented | — |
| **OCP, DIP, ISP, Encapsulation, Information Expert** | Ruby | `static_analyzer.rb` | Implemented but **not** merged into run_all output (only srp, lod, cmo, dry, lsp used) |

**Note**: LLM uses LoD > 4 levels; static uses ≥ 3. Thresholds are not aligned.

---

## DEPENDENCIES

### Python (requirements.txt)

- aiohttp >= 3.9.0
- beautifulsoup4 >= 4.12.0
- matplotlib >= 3.7.0
- pandas >= 2.0.0
- requests >= 2.28.0
- tqdm >= 4.66.0

### External Tools

- **Ollama**: Local LLM server at `localhost:11434`; must run `ollama serve` and `ollama pull <model>`.
- **Ruby**: For `static_analyzer.rb`, `ruby_parser.rb`. Uses `ruby` (not `bundle exec`) from `parsers/` cwd.
- **Node.js**: For `ts_parser.ts`; run via `npx tsx ts_parser.ts <file>`.
- **GitHub API**: For extraction; `GITHUB_TOKEN` or `GH_TOKEN` recommended (5000 req/hr vs 60 unauthenticated).

### Ruby (parsers/Gemfile)

- prism

### Node (parsers/package.json)

- typescript ^5.9.0
- tsx ^4.19.0 (dev)

---

## CURRENT PAIN POINTS

1. **LoD threshold mismatch**: LLM prompt says "> 4 levels"; static analyzers use ≥ 3. run_all merges static output; LLM is primary. Inconsistency may confuse comparison.

2. **Ruby subprocess**: `ruby_static_analyzer.py` invokes `ruby static_analyzer.rb` (not `bundle exec ruby`). If prism is only in `vendor/bundle`, this may fail. Controller.py also uses plain `ruby`.

3. **Static analyzer output**: `static_analyzer.rb` produces OCP, DIP, ISP, Information Expert, Encapsulation; `run_all.py` and `ruby_static_analyzer._normalize_schema` only pass through srp, lod, cmo, dry, lsp (and parse_errors). Extra detectors are unused.

4. **DRY detector (dry_detector.py)**: Comment says "Future enhancement: integrate flay (Ruby gem) for deeper Type-1/2/3 clone detection and jscpd (npm) for TypeScript."

5. **TypeScript SRP/DRY/LSP**: Not implemented; deferred to LLM.

6. **GitHub rate limits**: Without token, extraction fails. With token, secondary limits can still cause 403; `--delay` and `--concurrency` help.

7. **full_files empty**: Some projects have 0 full_files (e.g. rate limits, no .rb/.ts/.tsx changed). Evaluation falls back to diff-only; static analysis is empty.

8. **Duplicate project IDs**: Same project_id can appear multiple times (different PRs). analyze_evaluations treats each line as a separate record; no deduplication.

9. **evaluate_design_hybrid --no-static**: Strips full_files so static is skipped; LLM still runs. `--no-static` is not documented in main docstring.

10. **Year in filename**: analyze_evaluations infers year from `evaluations_hybrid-YYYY.jsonl`. evaluate_design_hybrid does not add year; user must name output file per year when processing per-year datasets.

11. **Chunking**: Code split at 50K chars; last batch gets summary/confidence. If a batch fails, that batch's counts are lost (no retry).

12. **ts_parser LoD false positives**: Regex matches `e.target.value`, `process.env.PUBLIC_URL`, `row.original.permissions.review` as chains; many are benign.

---

## ADDITIONAL CONTEXT

- **Thesis focus**: LLM is the primary violation detector. Static analysis is for reference and LLM-vs-static comparison (correlation ~0.73, ratio ~1.01).
- **Expertiza**: NCSU peer-review system; Rails backend, some TypeScript/React frontend. Projects are student PRs to Expertiza.
- **projects.csv**: One row per project; "links" column can contain multiple GitHub PR URLs and Wiki URLs.
- **Per-year workflow**: Extract with `--year 2025` → output `dataset-2025.jsonl`; evaluate with `--input dataset-2025.jsonl --output evaluations_hybrid-2025.jsonl`; analyze picks up all `evaluations_hybrid-*.jsonl`.
