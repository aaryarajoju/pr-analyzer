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
| **analyze_violations.py** | Quick violation counts (CSV/MD table) for meetings | `violations_report.csv`, `.md` |
| **evaluate_design.py** | Full pipeline with parsers; narrative evaluation | `evaluations.jsonl` |

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
| dataset_raw.jsonl | extract_data | One JSON object per project (diff, wiki, feedback) |
| violations_report.csv | analyze_violations | Table: project_id, SRP, DRY, LoD, CMO, total, confidence, summary |
| violations_report.md | analyze_violations | Markdown table + summary stats |
| evaluations.jsonl | evaluate_design | One JSON per project with narrative evaluation |

---

## Documentation

- **DESIGN.md** – System design, prompts, and how each principle is evaluated
- **new-design.md** – Planned hybrid static + LLM architecture (not yet implemented)
- **parsers/README.md** – Parser setup and usage

---

## Requirements

- Python 3.9+
- Ollama with a code-capable model
- `projects.csv` (not included; provide your own)
