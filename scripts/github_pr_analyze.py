#!/usr/bin/env python3
"""
GitHub Actions entrypoint: diff → static analysis → optional cloud LLM summary.

Environment (set in workflow):
  BASE_SHA, HEAD_SHA     – PR base and head SHAs (required in CI)
  GITHUB_WORKSPACE       – repo root (default: cwd)

Optional LLM (OpenAI-compatible Chat Completions API):
  DESIGN_CHECK_LLM_URL    – e.g. https://api.openai.com/v1/chat/completions
  DESIGN_CHECK_LLM_KEY    – Bearer token
  DESIGN_CHECK_LLM_MODEL  – e.g. gpt-4o-mini

Limits:
  DESIGN_CHECK_MAX_FILES       – default 40
  DESIGN_CHECK_MAX_FILE_BYTES  – default 262144
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from static_analyzer.run_all import run_static_analysis  # noqa: E402

TEXT_EXTENSIONS = {".rb", ".ts", ".tsx"}


def _run_git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        sys.exit(r.returncode or 1)
    return r.stdout


def _changed_paths(repo: Path, base_sha: str, head_sha: str, max_files: int) -> list[str]:
    out = _run_git(
        ["diff", "--name-only", "--diff-filter=ACM", base_sha, head_sha],
        cwd=repo,
    )
    paths = [p.strip() for p in out.splitlines() if p.strip()]
    analyzed: list[str] = []
    for p in paths:
        low = p.lower()
        if not any(low.endswith(ext) for ext in TEXT_EXTENSIONS):
            continue
        analyzed.append(p)
        if len(analyzed) >= max_files:
            break
    return analyzed


def _read_at_commit(repo: Path, commit: str, path: str, max_bytes: int) -> str | None:
    r = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=repo,
        capture_output=True,
    )
    if r.returncode != 0:
        return None
    data = r.stdout
    if len(data) > max_bytes:
        return data[:max_bytes].decode("utf-8", errors="replace") + "\n\n… [truncated]\n"
    return data.decode("utf-8", errors="replace")


def _diff_stat(repo: Path, base_sha: str, head_sha: str, max_chars: int) -> str:
    out = _run_git(
        ["diff", "--stat", base_sha, head_sha],
        cwd=repo,
    )
    out = out.strip()
    if len(out) > max_chars:
        return out[:max_chars] + "\n… [truncated]"
    return out


def _try_llm_summary(
    findings: dict,
    diff_stat: str,
) -> dict | None:
    url = (os.environ.get("DESIGN_CHECK_LLM_URL") or "").strip()
    key = (os.environ.get("DESIGN_CHECK_LLM_KEY") or "").strip()
    model = (os.environ.get("DESIGN_CHECK_LLM_MODEL") or "gpt-4o-mini").strip()
    if not url or not key:
        return None

    try:
        import urllib.error
        import urllib.request
    except ImportError:
        return None

    summary_payload = {
        k: findings.get(k, {})
        for k in (
            "lod",
            "long_chain",
            "cmo",
            "srp",
            "dry",
            "lsp",
            "god_object",
            "feature_envy",
            "long_method",
            "shotgun_surgery",
            "ocp",
            "dip",
            "information_expert",
        )
    }
    prompt = (
        "You assist code review. Given static design-heuristic findings (JSON) and a git diff stat, "
        "write a short markdown section for maintainers. "
        "Be concise; note these are heuristic signals not proof. "
        "If counts are zero, say so.\n\n"
        f"Diff stat:\n{diff_stat}\n\n"
        f"Findings JSON:\n{json.dumps(summary_payload, ensure_ascii=False)[:12000]}\n"
    )
    body = json.dumps(
        {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "Reply with markdown only, no code fence around the whole reply."},
                {"role": "user", "content": prompt},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        return {"error": str(e)}

    text = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    return {"markdown": text.strip()} if text else {"error": "empty LLM response"}


def _markdown_report(
    paths: list[str],
    findings: dict,
    llm: dict | None,
) -> str:
    lines: list[str] = [
        "## Design check (static heuristics)",
        "",
        f"Analyzed **{findings.get('files_analyzed', 0)}** file(s): "
        + (", ".join(f"`{p}`" for p in paths) or "—"),
        "",
    ]
    for key in (
        ("lod", "LoD"),
        ("long_chain", "Long chain"),
        ("cmo", "CMO"),
        ("srp", "SRP"),
        ("dry", "DRY"),
        ("lsp", "LSP"),
        ("god_object", "God object"),
        ("feature_envy", "Feature envy"),
        ("long_method", "Long method"),
        ("shotgun_surgery", "Shotgun surgery"),
        ("ocp", "OCP"),
        ("dip", "DIP"),
        ("information_expert", "Information expert"),
    ):
        k, title = key
        block = findings.get(k) or {}
        cnt = block.get("count", 0)
        lines.append(f"- **{title}**: count={cnt}")
    lines.append("")
    if llm:
        if "markdown" in llm:
            lines.append("### LLM summary")
            lines.append("")
            lines.append(llm["markdown"])
        elif "error" in llm:
            lines.append(f"_LLM step skipped or failed: {llm['error']}_")
    else:
        lines.append(
            "_LLM summary disabled. Set `DESIGN_CHECK_LLM_URL` and `DESIGN_CHECK_LLM_KEY` to enable._"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="PR design check for GitHub Actions")
    parser.add_argument("--repo", type=Path, default=None, help="Repo root (default: GITHUB_WORKSPACE or cwd)")
    parser.add_argument("--base", type=str, default=os.environ.get("BASE_SHA", ""))
    parser.add_argument("--head", type=str, default=os.environ.get("HEAD_SHA", ""))
    parser.add_argument("--json-out", type=Path, default=Path("pr-analyzer-report.json"))
    parser.add_argument("--md-out", type=Path, default=Path("pr-analyzer-report.md"))
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM even if env vars set")
    args = parser.parse_args()

    repo = args.repo or Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd()))
    base, head = args.base.strip(), args.head.strip()
    if not base or not head:
        print("BASE_SHA and HEAD_SHA must be set (or pass --base / --head).", file=sys.stderr)
        sys.exit(1)

    max_files = int(os.environ.get("DESIGN_CHECK_MAX_FILES", "40"))
    max_bytes = int(os.environ.get("DESIGN_CHECK_MAX_FILE_BYTES", str(256 * 1024)))

    _run_git(["rev-parse", "--verify", base], cwd=repo)
    _run_git(["rev-parse", "--verify", head], cwd=repo)

    paths = _changed_paths(repo, base, head, max_files)
    files: list[dict] = []
    for p in paths:
        content = _read_at_commit(repo, head, p, max_bytes)
        if content is None:
            continue
        if "\x00" in content:
            continue
        files.append({"path": p, "content": content})

    if not files:
        findings = run_static_analysis([])
        diff_stat = _diff_stat(repo, base, head, 8000)
        llm_out = None if args.no_llm else _try_llm_summary(findings, diff_stat)
        payload = {
            "meta": {
                "base_sha": base,
                "head_sha": head,
                "paths_attempted": paths,
                "note": "no analyzable .rb/.ts/.tsx files in diff (or all unreadable)",
            },
            "static_findings": findings,
            "diff_stat": diff_stat,
            "llm": llm_out,
        }
        args.json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        args.md_out.write_text(_markdown_report(paths, findings, llm_out), encoding="utf-8")
        return

    findings = run_static_analysis(files)
    diff_stat = _diff_stat(repo, base, head, 8000)
    llm_out = None if args.no_llm else _try_llm_summary(findings, diff_stat)

    payload = {
        "meta": {
            "base_sha": base,
            "head_sha": head,
            "files": [f["path"] for f in files],
        },
        "static_findings": findings,
        "diff_stat": diff_stat,
        "llm": llm_out,
    }
    args.json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    args.md_out.write_text(_markdown_report([f["path"] for f in files], findings, llm_out), encoding="utf-8")


if __name__ == "__main__":
    main()
