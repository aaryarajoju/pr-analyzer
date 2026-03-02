#!/usr/bin/env python3
"""
Controller script that runs the appropriate parser (Ruby or TypeScript) on a file
and prints the JSON output. Uses subprocess to invoke the parsers.

Usage: python controller.py <file_path>
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Directory containing this script and the parsers
PARSERS_DIR = Path(__file__).resolve().parent


def run_ruby_parser(file_path: str) -> dict:
    """Run the Ruby parser on a .rb file."""
    script = PARSERS_DIR / "ruby_parser.rb"
    env = {**os.environ, "BUNDLE_GEMFILE": str(PARSERS_DIR / "Gemfile")}
    result = subprocess.run(
        ["bundle", "exec", "ruby", str(script), file_path],
        capture_output=True,
        text=True,
        cwd=PARSERS_DIR,
        env=env,
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


def main():
    if len(sys.argv) < 2:
        print("Usage: python controller.py <file_path>", file=sys.stderr)
        sys.exit(1)

    file_path = sys.argv[1]
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
