# PR Analyzer: Design Document

## 1. Overview

The PR Analyzer is a thesis tool that evaluates code changes in pull requests against four software design principles: **SRP** (Single Responsibility), **DRY** (Don't Repeat Yourself), **LoD** (Law of Demeter), and **Class Method Overuse**. It combines automated code parsing with an LLM to produce actionable violation counts and summaries that can guide code review and refactoring.

**Goal:** Provide quantitative and qualitative design feedback on student/instructor PR submissions for the Expertiza project.

---

## 2. System Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  projects.csv   │────▶│  extract_data.py │────▶│  dataset_raw.jsonl  │
│  (links, IDs)   │     │  (GitHub + Wiki) │     │  (diffs, wiki, etc) │
└─────────────────┘     └──────────────────┘     └──────────┬──────────┘
                                                             │
                    ┌────────────────────────────────────────┤
                    │                                        │
                    ▼                                        ▼
         ┌─────────────────────┐                 ┌─────────────────────┐
         │  evaluate_design.py │                 │  analyze_violations │
         │  (full pipeline)    │                 │  (quick report)     │
         │  - Parsers (.rb/.ts)│                 │  - Diff + Wiki only │
         │  - LLM evaluation   │                 │  - Structured JSON  │
         │  - Stream to JSONL  │                 │  - CSV + Markdown   │
         └──────────┬──────────┘                 └──────────┬──────────┘
                    │                                        │
                    ▼                                        ▼
         ┌─────────────────────┐                 ┌─────────────────────┐
         │  evaluations.jsonl  │                 │  violations_report  │
         │  (per-project)      │                 │  .csv / .md         │
         └─────────────────────┘                 └─────────────────────┘
```

---

## 3. Data Flow

### 3.1 Input: projects.csv

A spreadsheet with columns:
- **Project ID** (e.g., E2541)
- **links** – URLs to GitHub PRs and Expertiza Wiki pages
- **Feedback on project** – instructor feedback text
- **Feedback on design doc** – design review feedback

### 3.2 Extraction (extract_data.py)

- **GitHub PRs:** Uses the GitHub API with `Accept: application/vnd.github.v3.diff` to fetch unified diffs
- **Wiki:** Fetches HTML pages and extracts main content via BeautifulSoup
- **Output:** `dataset_raw.jsonl` – one JSON object per line:
  ```json
  {
    "project_id": "E2541",
    "diff": "diff --git a/app/controllers/...",
    "wiki_content": "...",
    "project_feedback": "...",
    "design_feedback": "..."
  }
  ```
- Handles rate limits (optionally with `GITHUB_TOKEN`) and concurrent fetches

### 3.3 Parsers (parsers/)

Optional AST-based analysis for `.rb` and `.ts`/`.tsx` files:

| Parser      | Tech   | Output (JSON)                                      |
|-------------|--------|----------------------------------------------------|
| Ruby        | Prism  | classes, instance_methods, class_methods, external_calls |
| TypeScript  | ts-node| components, hooks, imports                        |

- Run via `parsers/controller.py` on a file path
- **Limitation:** Diffs contain *fragments*, not full files; parsers often fail on incomplete syntax. The LLM receives the diff directly as primary input.

### 3.4 LLM Integration (Ollama)

- **Endpoint:** `http://localhost:11434/api/generate`
- **Model:** Configurable (default: `deepseek-coder-v2:16b-lite-instruct-q4_K_M`)
- **Input to LLM:**
  - Wiki/design documentation
  - Code diff (unified format)
  - (Optionally) Parser output when available

---

## 4. Design Principles Evaluated

| Principle | Abbrev | Definition |
|-----------|--------|------------|
| **Single Responsibility** | SRP | Each class/module has one reason to change |
| **Don't Repeat Yourself** | DRY | No unnecessary duplication |
| **Law of Demeter** | LoD | Avoid long chains of method calls on other objects |
| **Class Method Overuse** | CMO | Class methods vs instance methods used appropriately |

The LLM is prompted to count violations for each principle and produce a confidence score (1–5) and a short summary.

### 4.1 How Each Principle Is Evaluated

#### SRP (Single Responsibility Principle)

**Criterion:** Does each class/module have a single reason to change?

**What the LLM looks for:**
- **Violations:** A class or module that handles multiple unrelated concerns (e.g., both persistence and business logic, or both validation and formatting)
- **God classes:** Large classes with many methods spanning different domains
- **Mixed concerns:** Controllers that perform data transformation, validation, and external API calls in one place
- **Example violation:** A `UserController` that both authenticates users and sends email notifications

**Signals in the diff:** Methods doing unrelated tasks, large method counts, cross-domain imports or external calls

---

#### DRY (Don't Repeat Yourself)

**Criterion:** Is there unnecessary duplication?

**What the LLM looks for:**
- **Violations:** Same or nearly identical logic repeated in multiple places
- **Copy-paste code:** Similar blocks with minor variations (e.g., different field names)
- **Repeated patterns:** Identical validation, formatting, or initialization logic in different methods/files
- **Example violation:** Five methods each with the same 10-line error-handling block

**Signals in the diff:** Similar code blocks, repeated string literals, duplicated conditionals or loops

---

#### LoD (Law of Demeter)

**Criterion:** Are there long chains of method calls on other objects?

**What the LLM looks for:**
- **Violations:** Chained method calls reaching through multiple objects (e.g., `a.getB().getC().doSomething()`)
- **Prop drilling:** Passing objects through layers only to access a nested property
- **Train wrecks:** Expressions like `user.profile.address.city` or `response.data.items[0].name`
- **Example violation:** `assignment.participants.find_by(user_id: id).team.members.first`

**Signals in the diff:** Dot chains longer than 2–3 levels, accessing internals of returned objects

---

#### Class Method Overuse (CMO)

**Criterion:** Are class methods used appropriately vs instance methods?

**What the LLM looks for:**
- **Violations:** Logic that depends on instance state but is implemented as a class method
- **Utility abuse:** Classes used as namespaces for functions that could be instance methods
- **Missing context:** Class methods that receive many parameters that could be instance attributes
- **Example violation:** `User.validate_email(email)` and `User.validate_password(pw)` when validation could be `user.valid?` on an instance

**Signals in the diff:** Many `self.` or `ClassName.` calls, static-style methods that don't use `self`/instance state

---

### 4.2 Prompt Examples

The following are the actual prompts sent to the LLM. Placeholders like `{wiki}`, `{diff}`, and `{parser_str}` are filled with project-specific data.

#### evaluate_design.py (Full Pipeline, Free-Form)

Produced by `build_prompt()`. Output is free-form text.

```
You are evaluating code design principles. Analyze the following and provide a concise evaluation for each principle.

## Parser Output (extracted structure from the code)
{parser_str}

## Wiki / Design Documentation
{wiki_content or "(none)"}

## Code Diff
{diff or "(empty)"}

---

Evaluate the code changes for these design principles:
1. **SRP (Single Responsibility Principle)** - Does each class/module have a single reason to change?
2. **DRY (Don't Repeat Yourself)** - Is there unnecessary duplication?
3. **LoD (Law of Demeter)** - Are there long chains of method calls on other objects?
4. **Class Method Overuse** - Are class methods used appropriately vs instance methods?

Provide a brief assessment for each principle (1-3 sentences) and an overall design quality score (1-5) with brief justification.
```

- **`{parser_str}`** – JSON from Ruby/TypeScript parsers when available; otherwise `{}`
- **`{wiki_content}`** – Extracted text from Expertiza Wiki; `"(none)"` if empty
- **`{diff}`** – Unified diff from the GitHub PR

---

#### analyze_violations.py (Structured JSON, Violation Counts)

Used for advisor reports. Forces JSON output via `format: "json"` in the Ollama request.

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

- **`{wiki}`** – Same as `wiki_content` above
- **`{diff}`** – Same as above; truncated to ~12K chars if very long

**Differences between the two prompts:**

| Aspect | evaluate_design | analyze_violations |
|--------|-----------------|-------------------|
| Parser output | Included | Omitted |
| Output format | Free-form text | Strict JSON |
| Use case | Rich narrative evaluation | Quantitative report, CSV/table |
| Ollama `format` | Not set | `"json"` |

---

## 5. Scripts

### 5.1 evaluate_design.py (Full Pipeline)

- Reads `dataset_raw.jsonl` line by line (no full-file load)
- Detects `.rb` / `.ts` / `.tsx` files in diffs and runs parsers when possible
- Builds a prompt with parser output (if any), wiki, and diff
- Calls Ollama for a free-form design evaluation
- **Writes each result to `evaluations.jsonl` immediately** (crash-safe)

```bash
python evaluate_design.py --input dataset_raw.jsonl --output evaluations.jsonl --model deepseek-coder-v2:16b-lite-instruct-q4_K_M
```

### 5.2 analyze_violations.py (Quick Report)

- Designed for advisor demos and meetings
- Processes first N records (default: 50)
- Uses a **structured JSON prompt** with `format: "json"` for reliable parsing
- Produces:
  - **CSV** – project_id, SRP, DRY, LoD, ClassMethodOveruse, total_violations, confidence, summary
  - **Markdown** – table + aggregate stats (total violations, averages)
  - **Console** – preview of first 15 rows

```bash
python analyze_violations.py --limit 50 -o violations_report.csv -m deepseek-coder-v2:16b-lite-instruct-q4_K_M
```

---

## 6. Outputs

### 6.1 evaluations.jsonl

One JSON object per project:
```json
{
  "project_id": "E2541",
  "parser_output": { "ruby": [...], "typescript": [...] },
  "evaluation": "Free-form LLM text evaluating SRP, DRY, LoD, Class Method Overuse..."
}
```

### 6.2 violations_report.csv / .md

| project_id | SRP | DRY | LoD | ClassMethodOveruse | total_violations | confidence | summary |
|------------|-----|-----|-----|--------------------|-----------------|-----------|---------|
| E2542 | 0 | 0 | 0 | 0 | 0 | 3 | No violations detected... |
| E2541 | 0 | 14 | 5 | 2 | 21 | 3 | DRY violations with repeated logic... |

The Markdown file adds summary statistics (total violations across projects, averages).

---

## 7. Prerequisites

- **Python 3.9+** with dependencies: `requests`, `aiohttp`, `beautifulsoup4`, `pandas`, `tqdm`
- **Ollama** running locally with a code-capable model (e.g., DeepSeek Coder, Llama, Mistral)
- **Parsers (optional):** For Ruby: `bundle install` in `parsers/`; for TypeScript: `npm install` in `parsers/`

---

## 8. Limitations and Future Work

1. **Diff fragments:** Parsers need complete files; diffs provide only changed hunks. Parser output is often empty; the LLM relies mainly on the diff text.
2. **LLM variability:** Counts can vary across runs; confidence scores help indicate reliability.
3. **Model choice:** Code models (e.g., DeepSeek Coder) tend to perform better than general-purpose models for this task.
4. **Scaling:** Processing 1000+ projects requires time (roughly 15–30 sec per project) and optional resumability (e.g., `--skip` in `evaluate_design.py`).

---

## 9. Summary

The PR Analyzer extracts code changes from GitHub PRs and Expertiza Wiki links, combines them with optional parser output, and uses a local LLM to produce design principle violation counts and textual feedback. The `analyze_violations` script provides a focused, table-style report suitable for advisor reviews and thesis evaluation of design quality in student submissions.
