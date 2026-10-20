#!/usr/bin/env python3
"""Build the judge input bundle (PR text, patches, tests)."""
from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

from foresight.config import TARGETS
from foresight.execution.runner import is_test_path

ROOT = Path(__file__).resolve().parents[1]
REPOS = ROOT / "repos"
CACHE = ROOT / "data" / "github_cache"
DATASET = ROOT / "data" / "dataset.jsonl"
OUT_JSONL = ROOT / "data" / "judge_poc.jsonl"
OUT_CSV = ROOT / "data" / "judge_poc.csv"


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace",
    )
    return p.stdout


def build_pr_index(repo_short: str) -> dict[int, dict]:
    """number -> {title, body} from all cached GitHub API responses."""
    idx: dict[int, dict] = {}
    cdir = CACHE / repo_short
    if not cdir.is_dir():
        return idx
    for f in cdir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            num = it.get("number")
            if not isinstance(num, int):
                continue
            # keep the richest title/body
            if "title" in it or "body" in it:
                prev = idx.get(num)
                cand = {"title": it.get("title") or "", "body": it.get("body") or ""}
                if prev is None or (len(cand["body"]) > len(prev.get("body", ""))):
                    idx[num] = cand
    return idx


def split_patch(repo: Path, base: str, head: str) -> tuple[str, str]:
    """(code, test) patches; binaries dropped."""
    if not base or not head:
        return "", ""
    binary = {ln.split("\t", 2)[2] for ln in
              git(repo, "diff", "--numstat", f"{base}..{head}").splitlines()
              if ln.startswith("-\t-\t")}
    names = [n for n in git(repo, "diff", "--name-only", f"{base}..{head}").splitlines()
             if n.strip() and n not in binary]
    if not names:
        return "", ""
    code_files = [n for n in names if not is_test_path(n)]
    test_files = [n for n in names if is_test_path(n)]
    code = git(repo, "diff", f"{base}..{head}", "--", *code_files) if code_files else ""
    test = git(repo, "diff", f"{base}..{head}", "--", *test_files) if test_files else ""
    return code, test


def commit_message(repo: Path, ref: str) -> str:
    """Full commit message for a ref ('' if unavailable)."""
    return git(repo, "show", "-s", "--format=%B", ref).strip() if ref else ""


_GH_CACHE: dict[tuple[str, int], dict] = {}


def gh_pr_text(slug: str, num: int) -> dict | None:
    """PR (title, body) via `gh`, cached."""
    key = (slug, num)
    if key in _GH_CACHE:
        return _GH_CACHE[key]
    p = subprocess.run(
        ["gh", "api", f"repos/{slug}/pulls/{num}", "--jq", "{title,body}"],
        capture_output=True, text=True, errors="replace",
    )
    rec = None
    if p.returncode == 0 and p.stdout.strip():
        try:
            d = json.loads(p.stdout)
            rec = {"title": d.get("title") or "", "body": d.get("body") or ""}
        except Exception:
            rec = None
    _GH_CACHE[key] = rec
    return rec


def pr_text(idx: dict[int, dict], num: int, slug: str = "") -> tuple[str, str]:
    rec = idx.get(num) or {}
    title, body = rec.get("title", ""), rec.get("body", "")
    if not (title or body) and slug:
        fetched = gh_pr_text(slug, num)
        if fetched:
            title, body = fetched["title"], fetched["body"]
    return title, body


def fmt_tests(tests: list) -> str:
    """[[classname, name], ...] -> 'classname::name;classname::name'."""
    out = []
    for t in tests or []:
        if isinstance(t, (list, tuple)):
            out.append("::".join(str(x) for x in t))
        else:
            out.append(str(t))
    return ";".join(out)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default=str(DATASET))
    ap.add_argument("--out", default=str(OUT_JSONL))
    args = ap.parse_args()
    out_jsonl = Path(args.out)
    rows = [json.loads(l) for l in Path(args.chains).read_text().splitlines() if l.strip()]
    pr_indexes: dict[str, dict[int, dict]] = {}
    records = []

    for r in rows:
        short = r["repo"]
        slug = getattr(TARGETS.get(short), "slug", short)
        repo = REPOS / short
        if short not in pr_indexes:
            pr_indexes[short] = build_pr_index(short)
        idx = pr_indexes[short]

        pr1, pr2 = r["earlier_pr"], r["later_pr"]
        runner = getattr(TARGETS.get(short), "runner", "pytest")
        if runner == "django":
            # django: commit message is the PR text
            msg1 = commit_message(repo, r.get("pr1_head_sha", ""))
            msg2 = commit_message(repo, r.get("pr2_head_sha", ""))
            # title = first line, body = rest
            pr1_title = msg1.splitlines()[0] if msg1 else ""
            pr2_title = msg2.splitlines()[0] if msg2 else ""
            pr1_body = "\n".join(msg1.splitlines()[1:]).strip()
            pr2_body = "\n".join(msg2.splitlines()[1:]).strip()
        else:
            pr1_title, pr1_body = pr_text(idx, pr1, slug)
            pr2_title, pr2_body = pr_text(idx, pr2, slug)

        pr1_code, pr1_test = split_patch(repo, r.get("pr1_base_sha", ""), r.get("pr1_head_sha", ""))
        pr2_code, pr2_test = split_patch(repo, r.get("pr2_base_sha", ""), r.get("pr2_head_sha", ""))

        rec = {
            "instance_id": f"{short}__{pr1}-{pr2}",
            "repo": slug,
            "repo_short": short,
            "earlier_pr": pr1,
            "later_pr": pr2,
            "category": r.get("category", ""),
            "evidence": r.get("evidence", ""),
            # --- prior labels ---
            "prior_quality": r.get("quality", ""),
            "prior_judge_consensus": r.get("judge_consensus", ""),
            "mutation_score": r.get("mutation_score", ""),
            # --- SHAs ---
            "pr1_base_sha": r.get("pr1_base_sha", ""),
            "pr1_head_sha": r.get("pr1_head_sha", ""),
            "pr2_base_sha": r.get("pr2_base_sha", ""),
            "pr2_head_sha": r.get("pr2_head_sha", ""),
            # --- PR1, the task ---
            "pr1_title": pr1_title,
            "pr1_problem_statement": (pr1_title + "\n\n" + pr1_body).strip(),
            "pr1_patch": pr1_code,
            "pr1_test_patch": pr1_test,
            "pr1_fail_to_pass": fmt_tests(r.get("pr1_f2p_tests", [])),
            # --- PR2, the fix ---
            "pr2_title": pr2_title,
            "pr2_problem_statement": (pr2_title + "\n\n" + pr2_body).strip(),
            "pr2_patch": pr2_code,
            "pr2_test_patch": pr2_test,
            "pr2_fail_to_pass": fmt_tests(r.get("anticipation_tests", [])),
        }
        records.append(rec)

    # JSONL
    with out_jsonl.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    # ---- coverage report ----
    n = len(records)
    miss_pr1_body = sum(1 for r in records if not r["pr1_problem_statement"])
    miss_pr2_body = sum(1 for r in records if not r["pr2_problem_statement"])
    miss_pr1_code = sum(1 for r in records if not r["pr1_patch"])
    miss_pr2_code = sum(1 for r in records if not r["pr2_patch"])
    miss_pr2_test = sum(1 for r in records if not r["pr2_test_patch"])
    print(f"wrote {out_jsonl}  ({n} chains)")
    print(f"  missing PR1 description : {miss_pr1_body}/{n}")
    print(f"  missing PR2 description : {miss_pr2_body}/{n}")
    print(f"  missing PR1 code patch  : {miss_pr1_code}/{n}")
    print(f"  missing PR2 code patch  : {miss_pr2_code}/{n}")
    print(f"  missing PR2 test patch  : {miss_pr2_test}/{n}")


if __name__ == "__main__":
    main()
