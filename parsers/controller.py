#!/usr/bin/env python3
"""
Controller script that runs the appropriate parser (Ruby or TypeScript) on a file
and prints the JSON output. Uses subprocess to invoke the parsers.

Usage:
    python controller.py <file_path>           # standard parser (structure only)
    python controller.py --static <file_path>  # full static analysis (SRP/LoD/CMO/DRY/LSP)
    python controller.py --static <f1> <f2>   # static analysis on multiple Ruby files
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Directory containing this script and the parsers
PARSERS_DIR = Path(__file__).resolve().parent


def run_ruby_parser(file_path: str) -> dict:
    """Run the extended Ruby parser on a .rb file (structure + LoD/CMO fields)."""
    script = PARSERS_DIR / "ruby_parser.rb"
    result = subprocess.run(
        ["ruby", str(script), file_path],
        capture_output=True,
        text=True,
        cwd=PARSERS_DIR,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout)


def run_ts_parser(file_path: str) -> dict:
    """Run the TypeScript parser on a .ts or .tsx file."""
    script = PARSERS_DIR / "ts_parser.ts"
    env = {**os.environ, "NODE_OPTIONS": "--no-warnings"}
    result = subprocess.run(
        ["npx", "tsx", str(script), file_path],
        capture_output=True,
        text=True,
        cwd=PARSERS_DIR,
        env=env,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout)


def run_static_analyzer(file_paths: list[str]) -> dict:
    """
    Run the full static analyzer (SRP, LoD, CMO, DRY, LSP) on one or more Ruby files.
    Uses parsers/static_analyzer.rb via Prism.
    Returns a combined findings dict per NEW-DESIGN.md §5.6.
    """
    script = PARSERS_DIR / "static_analyzer.rb"

    ruby_files = [f for f in file_paths if Path(f).suffix.lower() == ".rb"]
    if not ruby_files:
        print("No .rb files provided for static analysis.", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        ["ruby", str(script), *ruby_files],
        capture_output=True,
        text=True,
        cwd=PARSERS_DIR,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout)


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage: python controller.py <file_path>", file=sys.stderr)
        print("       python controller.py --static <file.rb> [<file.rb> ...]", file=sys.stderr)
        sys.exit(1)

    # ── Static analysis mode ─────────────────────────────────────────────────
    if args[0] == "--static":
        file_paths = args[1:]
        if not file_paths:
            print("--static requires at least one file path.", file=sys.stderr)
            sys.exit(1)
        for fp in file_paths:
            if not Path(fp).exists():
                print(f"File not found: {fp}", file=sys.stderr)
                sys.exit(1)
        output = run_static_analyzer(file_paths)
        print(json.dumps(output, indent=2))
        return

    # ── Standard parser mode (single file) ──────────────────────────────────
    file_path = args[0]
    path = Path(file_path)

    if not path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    suffix = path.suffix.lower()

    if suffix == ".rb":
        output = run_ruby_parser(file_path)
    elif suffix in (".ts", ".tsx"):
        output = run_ts_parser(file_path)
    else:
        print(f"Unsupported file type: {suffix}. Use .rb, .ts, or .tsx", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
