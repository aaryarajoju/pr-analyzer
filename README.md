# pr-analyzer

Thesis project to analyze design principles (SRP, DRY, LoD, Class Method Overuse) in pull requests using an LLM.

---

## Quick Start

```bash
# 1. Setup
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Extract data (requires projects.csv with PR links)
python extract_data.py --input projects.csv --output dataset_raw.jsonl

# 3. Install and run Ollama (see Ollama Setup below)
ollama pull deepseek-coder-v2:16b-lite-instruct-q4_K_M
ollama serve   # if not already running

# 4. Run violation analysis (produces CSV + Markdown table)
python analyze_violations.py --limit 50 -o violations_report.csv
```

---

## Ollama Setup

The analysis scripts use [Ollama](https://ollama.com) to run a local LLM. You must have Ollama installed and a model pulled.

1. **Install Ollama:** https://ollama.com/download
2. **Pull a model:**
   ```bash
   ollama pull deepseek-coder-v2:16b-lite-instruct-q4_K_M
   # Or: ollama pull llama3.2  ollama pull mistral
   ```
3. **Start Ollama** (usually runs automatically; if not):
   ```bash
   ollama serve
   ```
4. **List models:** `ollama list`

The default model is `deepseek-coder-v2:16b-lite-instruct-q4_K_M`. Use `--model <name>` to override.

---

## Scripts

| Script | Purpose | Output |
|--------|---------|--------|
| **extract_data.py** | Fetch PR diffs and Wiki content from GitHub | `dataset_raw.jsonl` |
| **extract_data_v2.py** | Same + full file content for static analysis | `dataset_v2.jsonl` |
| **analyze_violations.py** | Quick violation counts (CSV/MD table) for meetings | `violations_report.csv`, `.md` |
| **evaluate_design.py** | Full pipeline with parsers; narrative evaluation | `evaluations.jsonl` |
| **evaluate_design_hybrid.py** | Hybrid: static analysis → LLM validation | `evaluations_hybrid.jsonl` |

### extract_data.py

Extracts PR diffs and Wiki content from `projects.csv` into `dataset_raw.jsonl`.

```bash
python extract_data.py --input projects.csv --output dataset_raw.jsonl
```

- `-i, --input`  Path to projects.csv (default: `projects.csv`)
- `-o, --output` Output JSONL file (default: `dataset_raw.jsonl`)

**GitHub rate limits:** Unauthenticated = 60/hour. Set `GITHUB_TOKEN` or `GH_TOKEN` for 5000/hour:

```bash
export GITHUB_TOKEN=ghp_your_token_here
python extract_data.py
```

### analyze_violations.py

Produces a table of violation counts (SRP, DRY, LoD, Class Method Overuse) for the first N records. Best for advisor meetings and quantitative reports.

```bash
python analyze_violations.py --limit 50 -o violations_report.csv -m deepseek-coder-v2:16b-lite-instruct-q4_K_M
```

- `-i, --input`  Input JSONL (default: `dataset_raw.jsonl`)
- `-n, --limit`  Number of records (default: 50)
- `-o, --output` Output CSV path (also writes `.md`)
- `-m, --model`  Ollama model name

### evaluate_design.py

Runs parsers (when possible) and sends diff + parser output + wiki to the LLM for a free-form evaluation. Writes each result immediately (crash-safe).

```bash
python evaluate_design.py --input dataset_raw.jsonl --output evaluations.jsonl
```

- `-i, --input`  Input JSONL
- `-o, --output` Output JSONL
- `-m, --model`  Ollama model
- `--skip`       Skip first N lines (for resuming)

---

## Hybrid Static + LLM Pipeline (NEW-DESIGN.md)

The hybrid pipeline adds deterministic static analysis before the LLM step.

### Recommended workflow

```bash
# Step 1 – Extract data + full file content for changed paths
python extract_data_v2.py --input projects.csv --output dataset_v2.jsonl

# Step 2 – Run hybrid analysis (static first, then LLM validates)
python evaluate_design_hybrid.py --input dataset_v2.jsonl --output evaluations_hybrid.jsonl

# Optional: limit to first 10 projects for testing
python evaluate_design_hybrid.py --input dataset_v2.jsonl -n 10

# Fallback: diff-only + LLM-only mode (no static analysis)
python evaluate_design_hybrid.py --input dataset_raw.jsonl --no-static
```

### Static analysis only (no LLM, for debugging)

```bash
# Run on a specific Ruby file
cd parsers
python controller.py --static path/to/file.rb

# Or via the Python module
python -m static_analyzer.run_all -f my_files.json
```

### Principles detected

| Principle | Static | LLM role |
|-----------|--------|----------|
| SRP | Method count + instantiation count | Validates + refines |
| DRY | Structural-hash clone detection | Validates + extends |
| LoD | Regex call-chain depth (≥ 3 levels) | Validates |
| CMO | Class-method / total-method ratio | Validates |
| LSP | Arity mismatch in overrides | Validates (LLM-primary) |

---

## projects.csv Format

Required columns:

| Column | Description |
|--------|-------------|
| Project ID | Unique identifier (e.g., E2541) |
| links | URLs to GitHub PRs and Expertiza Wiki pages |
| Feedback on project | Instructor feedback text |
| Feedback on design doc | Design review feedback |

The `links` column should contain GitHub PR URLs (e.g. `https://github.com/expertiza/expertiza/pull/123`) and optionally Wiki URLs. The script extracts these and fetches the content.

---

## Parsers (Optional)

The parsers extract structure from Ruby and TypeScript files. They are used by `evaluate_design.py` when diff fragments are parseable (often they are not). See `parsers/README.md` for setup:

```bash
cd parsers
bundle config set --local path 'vendor/bundle'
bundle install
npm install
```

---

## Output Files

| File | Produced by | Description |
|------|-------------|-------------|
| dataset_raw.jsonl | extract_data | One JSON per project (diff, wiki, feedback) |
| dataset_v2.jsonl | extract_data_v2 | Same + `full_files` (full changed file content) |
| violations_report.csv | analyze_violations | LLM-only: SRP/DRY/LoD/CMO counts |
| violations_report.md | analyze_violations | Markdown table + aggregate stats |
| evaluations.jsonl | evaluate_design | Free-form narrative evaluation (diff-only) |
| evaluations_hybrid.jsonl | evaluate_design_hybrid | Hybrid: static findings + LLM validation |

---

## Documentation

- **OLD-DESIGN.md** – Original system design, prompts, and how each principle is evaluated
- **NEW-DESIGN.md** – Hybrid static + LLM architecture specification (implemented)
- **parsers/README.md** – Parser setup and usage

---

## Requirements

- Python 3.9+
- Ollama with a code-capable model
- `projects.csv` (not included; provide your own)
