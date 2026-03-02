#!/usr/bin/env python3
"""
Analyze first N records for design violations. Outputs a table (CSV + Markdown)
with per-project violation counts, confidence, and summary for advisor review.
"""

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
DEFAULT_MODEL = "deepseek-coder-v2:16b-lite-instruct-q4_K_M"  # or deepseek-llm, llama3.2, etc.

VIOLATION_PROMPT = """Analyze this code diff for design principle violations.

## Wiki / Design Documentation
{wiki}

## Code Diff
{diff}

---

Return ONLY valid JSON (no markdown, no extra text) with this exact structure:
{{
  "violations": {{
    "SRP": <number of SRP violations, 0 if none>,
    "DRY": <number of DRY violations>,
    "LoD": <number of Law of Demeter violations>,
    "ClassMethodOveruse": <number of class method overuse violations>
  }},
  "total_violations": <sum of all violations>,
  "confidence": <1-5, how confident you are in this analysis>,
  "summary": "<1-2 sentence summary of main issues>"
}}

Evaluate: SRP (Single Responsibility), DRY (Don't Repeat Yourself), LoD (Law of Demeter), Class Method Overuse."""


MODEL_ALIASES = {
    "deepseek": "deepseek-coder-v2:16b-lite-instruct-q4_K_M",
    "deepseek-llm": "deepseek-coder-v2:16b-lite-instruct-q4_K_M",
}  # alias -> Ollama model name


def check_ollama(model: str) -> None:
    """Verify Ollama is running and model exists. Exit with clear instructions if not."""
    try:
        r = requests.get(OLLAMA_TAGS_URL, timeout=5)
        r.raise_for_status()
        all_names = [m["name"] for m in r.json().get("models", [])]
        models_base = {n.split(":")[0] for n in all_names}
        found = model in all_names or model in models_base or any(
            n.startswith(model) or model in n for n in all_names
        )
        if not found:
            avail = ", ".join(all_names[:5]) or "(none)"
            logger.error(
                'Model "%s" not found. Available: %s. Run: ollama pull %s',
                model, avail, model,
            )
            sys.exit(1)
    except requests.RequestException as e:
        logger.error(
            "Cannot reach Ollama at localhost:11434. Is it running? Start with: ollama serve",
        )
        sys.exit(1)


def call_ollama(prompt: str, model: str = DEFAULT_MODEL, timeout: int = 90) -> str:
    """Call Ollama with JSON format for structured output."""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.RequestException as e:
        logger.error("Ollama error: %s", e)
        raise


def parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze violations for first N records")
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=PROJECT_ROOT / "dataset_raw.jsonl",
        help="Input JSONL",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=50,
        help="Number of records to process",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=PROJECT_ROOT / "violations_report.csv",
        help="Output CSV path",
    )
    parser.add_argument(
        "--model",
        "-m",
        type=str,
        default=DEFAULT_MODEL,
        help="Ollama model",
    )
    args = parser.parse_args()

    if not args.input.exists():
        logger.error("Input not found: %s", args.input)
        sys.exit(1)

    records: list[dict] = []
    with open(args.input, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= args.limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    logger.info("Processing %d records", len(records))
    model = MODEL_ALIASES.get(args.model, args.model)
    check_ollama(model)
    results: list[dict] = []

    for idx, rec in enumerate(records):
        project_id = rec.get("project_id", f"row_{idx+1}")
        diff = rec.get("diff", "")
        wiki = rec.get("wiki_content", "")

        # Truncate very long diffs to avoid token limits
        if len(diff) > 12000:
            diff = diff[:12000] + "\n\n... (truncated)"

        logger.info("Analyzing %s (%d/%d)", project_id, idx + 1, len(records))

        prompt = VIOLATION_PROMPT.format(wiki=wiki or "(none)", diff=diff or "(empty)")
        try:
            raw = call_ollama(prompt, model=model)
            data = parse_llm_json(raw)
        except Exception as e:
            logger.warning("Failed for %s: %s", project_id, e)
            data = {"violations": {}, "total_violations": 0, "confidence": 0, "summary": str(e)}

        v = data.get("violations", {})
        results.append({
            "project_id": project_id,
            "SRP": v.get("SRP", 0),
            "DRY": v.get("DRY", 0),
            "LoD": v.get("LoD", 0),
            "ClassMethodOveruse": v.get("ClassMethodOveruse", 0),
            "total_violations": data.get("total_violations", 0),
            "confidence": data.get("confidence", 0),
            "summary": data.get("summary", ""),
        })

    # Write CSV
    out_path = args.output
    fieldnames = ["project_id", "SRP", "DRY", "LoD", "ClassMethodOveruse", "total_violations", "confidence", "summary"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    logger.info("Wrote %s", out_path)

    # Write Markdown table
    md_path = out_path.with_suffix(".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Design Violation Analysis (First {} Records)\n\n".format(len(results)))
        f.write("| Project | SRP | DRY | LoD | Class Meth | Total | Conf | Summary |\n")
        f.write("|---------|-----|-----|-----|------------|-------|------|--------|\n")
        for r in results:
            summary = (r["summary"] or "")[:60].replace("|", "/").replace("\n", " ")
            if len(r["summary"] or "") > 60:
                summary += "..."
            f.write("| {} | {} | {} | {} | {} | {} | {} | {} |\n".format(
                r["project_id"], r["SRP"], r["DRY"], r["LoD"], r["ClassMethodOveruse"],
                r["total_violations"], r["confidence"], summary
            ))
        f.write("\n\n## Summary Stats\n\n")
        totals = [r["total_violations"] for r in results]
        confs = [r["confidence"] for r in results if r["confidence"]]
        f.write("- **Total violations across projects:** {}\n".format(sum(totals)))
        f.write("- **Avg violations per project:** {:.1f}\n".format(sum(totals) / len(totals) if totals else 0))
        f.write("- **Avg confidence:** {:.1f}\n".format(sum(confs) / len(confs) if confs else 0))
    logger.info("Wrote %s", md_path)

    # Print table to stdout
    print("\n" + "=" * 80)
    print("VIOLATIONS TABLE (first 15 rows)")
    print("=" * 80)
    for r in results[:15]:
        print("{:8} | SRP:{:2} DRY:{:2} LoD:{:2} CMO:{:2} | total:{:3} conf:{} | {}".format(
            r["project_id"], r["SRP"], r["DRY"], r["LoD"], r["ClassMethodOveruse"],
            r["total_violations"], r["confidence"], (r["summary"] or "")[:50]
        ))
    print("\nFull results: {} and {}".format(out_path, md_path))


if __name__ == "__main__":
    main()
