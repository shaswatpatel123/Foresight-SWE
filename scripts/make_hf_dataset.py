#!/usr/bin/env python3
"""Build the published dataset (SWE-bench style rows)."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from foresight import introducer_guard
from foresight.config import TARGETS

ROOT = Path(__file__).resolve().parents[1]
REPOS = ROOT / "repos"
DATASET = ROOT / "data" / "dataset.jsonl"
# judge bundles and results; last wins
BUNDLE = [ROOT / "data" / "judge_bundle_nv.jsonl",
          ROOT / "data" / "judge_bundle_pandas.jsonl",
          ROOT / "data" / "judge_poc.jsonl"]
JUDGE = [ROOT / "data" / "judge_nv_results.jsonl",
         ROOT / "data" / "judge_pandas_results.jsonl",
         ROOT / "data" / "judge_django_results.jsonl",
         ROOT / "data" / "judge_poc_results.jsonl"]
# PR1 F2P and P2P from the images
PR1_TESTS = ROOT / "data" / "pr1_tests.json"
# PR1 task code (A/B)
PR1_CODE = ROOT / "data" / "judge_pr1code_results.jsonl"
# repaired PR1 statements
SPEC_REPAIR = ROOT / "data" / "spec_repair_results.jsonl"
# PR1 difficulty
PR1_DIFFICULTY = ROOT / "data" / "pr1_difficulty_results.jsonl"
# severity labels
SEVERITY = ROOT / "data" / "severity_judge_deepseek-v4-flash.jsonl"
# PR2-direct statements and codes
PR2_STMT = ROOT / "data" / "pr2_problem_statements.jsonl"
PR2_CODE = ROOT / "data" / "judge_pr2_results.jsonl"
OUT_JSONL = ROOT / "data" / "foresight_dataset.jsonl"

# don't reproduce on amd64
ARCH_INCOMPATIBLE = {"pandas__64619-64643"}

# PR1 codes we drop outright
DROP_PR1_CODES = {"B1", "B4", "B11"}


def fmt_tests(tests) -> list[str]:
    out = []
    for t in tests or []:
        out.append("::".join(map(str, t)) if isinstance(t, (list, tuple)) else str(t))
    return out


def load_jsonl(paths) -> dict:
    """Merge JSONL files by id; later wins."""
    if isinstance(paths, Path):
        paths = [paths]
    merged: dict = {}
    for p in paths:
        if not p.exists():
            continue
        for l in p.read_text().splitlines():
            if l.strip():
                d = json.loads(l)
                merged[d["instance_id"]] = d
    return merged


def commit_date(repo_short: str, sha: str) -> str:
    """Commit date, or ""."""
    if not sha:
        return ""
    p = subprocess.run(
        ["git", "-C", str(REPOS / repo_short), "show", "-s", "--format=%cI", sha],
        capture_output=True, text=True, errors="replace",
    )
    return p.stdout.strip() if p.returncode == 0 else ""


def gap_days(d1: str, d2: str) -> str:
    from datetime import datetime
    try:
        a = datetime.fromisoformat(d1); b = datetime.fromisoformat(d2)
        return str(abs((b - a).days))
    except Exception:
        return ""


def _git(repo_short: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(REPOS / repo_short), *args],
                       capture_output=True, text=True, errors="replace")
    return p.stdout if p.returncode == 0 else ""


def commit_message(repo_short: str, sha: str) -> str:
    return _git(repo_short, "show", "-s", "--format=%B", sha).strip()


def resolve_sha(repo_short: str, ref: str) -> str:
    """Ref -> full SHA."""
    if not ref:
        return ref
    out = _git(repo_short, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    return out or ref


def split_diff(repo_short: str, base: str, head: str, is_test) -> tuple[str, str]:
    """(source, test) patches; binaries dropped."""
    # binary files show as "-\t-\t"
    binary = {ln.split("\t", 2)[2] for ln in
              _git(repo_short, "diff", "--numstat", base, head).splitlines()
              if ln.startswith("-\t-\t")}
    files = [f for f in _git(repo_short, "diff", "--name-only", base, head).splitlines()
             if f and f not in binary]
    src, tst = [], []
    for f in files:
        diff = _git(repo_short, "diff", base, head, "--", f)
        (tst if is_test(f) else src).append(diff)
    return "".join(src), "".join(tst)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="docker.io/shaswatpatel123",
                    help="image registry/namespace; image = <registry>/foresight:<repo>")
    ap.add_argument("--image-name", default="foresight")
    ap.add_argument("--exclude", default="urllib3",
                    help="comma-separated repos to drop from the dataset")
    ap.add_argument("--fully-gradeable", action="store_true",
                    help="keep only chains with BOTH PR1 FAIL_TO_PASS and PR2 anticipation F2P")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="include all rows (default)")
    g.add_argument("--eval-ready-only", action="store_true")
    args = ap.parse_args()
    drop_repos = {x.strip() for x in args.exclude.split(",") if x.strip()}

    rows = [json.loads(l) for l in DATASET.read_text().splitlines() if l.strip()]
    bundle = load_jsonl(BUNDLE)
    judge = load_jsonl(JUDGE)
    pr1_tests = json.loads(PR1_TESTS.read_text()) if PR1_TESTS.exists() else {}
    pr1_code = load_jsonl(PR1_CODE)
    repair = load_jsonl(SPEC_REPAIR)
    pr1_diff = load_jsonl(PR1_DIFFICULTY)
    severity = load_jsonl(SEVERITY)
    pr2_stmt = load_jsonl(PR2_STMT)
    pr2_code = load_jsonl(PR2_CODE)

    BUG = {"critical_bug", "major_bug", "minor_bug", "regression_bug",
           "edge_case_bug", "performance_bug", "security_bug"}

    # drop introducers after the report
    date_flags = introducer_guard.load_flags()
    n_date_dropped = 0

    records = []
    for r in rows:
        if r["repo"] in drop_repos:
            continue
        iid = f"{r['repo']}__{r['earlier_pr']}-{r['later_pr']}"
        if iid in ARCH_INCOMPATIBLE:  # fails on amd64
            continue
        if iid in date_flags:  # introducer after report
            n_date_dropped += 1
            continue
        b = bundle.get(iid, {})
        j = judge.get(iid, {})
        meta = j.get("meta") or {}
        cats = sorted(set(meta.get("pr_categories", []) or []))   # PR2 categories
        sev_v = severity.get(iid, {})                             # severity record
        sev_verdict = sev_v.get("verdict") or {}
        # consensus label, else from categories
        quality = r.get("quality", "")
        if quality in ("", "unreviewed"):
            catset = set(cats)
            quality = ("clean" if catset & BUG
                       else "flag_newfeature" if "core_feat" in catset
                       else "flag_doc" if "documentation_enh" in catset
                       else "unreviewed")
        clarity = j.get("clarity_score")
        # PR1 task code
        orig_code = ((pr1_code.get(iid, {}).get("pr1_meta") or {}).get("code"))
        if orig_code in DROP_PR1_CODES:   # drop
            continue
        task_code = orig_code
        # use the repaired statement if kept
        rep = repair.get(iid) or {}
        repaired_stmt = ""
        spec_repaired = bool(rep.get("kept"))
        if spec_repaired:
            task_code = rep.get("new_code") or task_code
            if rep.get("new_clarity") is not None:
                clarity = rep["new_clarity"]
            repaired_stmt = rep.get("repaired_problem_statement") or ""
        eval_ready = (bool(set(cats) & BUG)        # PR2 is a bug
                      and task_code == "A"          # PR1 task solvable
                      and clarity in (0, 1))         # PR1 statement clear
        if args.eval_ready_only and not eval_ready:
            continue

        t = TARGETS[r["repo"]]
        extra = list(t.install_args[2:])
        addopts_clear = bool(getattr(t, "clear_addopts", False))
        image = f"{args.registry.rstrip('/')}/{args.image_name}:{r['repo']}"
        ad = " -o addopts=" if addopts_clear else ""
        d1 = commit_date(r["repo"], r.get("pr1_head_sha", ""))
        d2 = commit_date(r["repo"], r.get("pr2_head_sha", ""))
        is_django = getattr(t, "runner", "pytest") == "django"

        # django: patches from git, PR1 message
        if is_django and not b:
            from foresight.execution.runner import is_test_path
            pr1_src, pr1_tst = split_diff(r["repo"], r["pr1_base_sha"], r["pr1_head_sha"], is_test_path)
            pr2_src, pr2_tst = split_diff(r["repo"], r["pr2_base_sha"], r["pr2_head_sha"], is_test_path)
            b = {
                "pr1_patch": pr1_src, "pr1_test_patch": pr1_tst,
                "pr2_patch": pr2_src, "pr2_test_patch": pr2_tst,
                "pr1_problem_statement": commit_message(r["repo"], r["pr1_head_sha"]),
            }
            # '<sha>^' -> full SHA
            for k in ("pr1_base_sha", "pr2_base_sha", "pr1_head_sha", "pr2_head_sha"):
                r[k] = resolve_sha(r["repo"], r.get(k, ""))

        rec = {
            # ---- SWE-bench fields (PR1 task) ----
            "instance_id": iid,
            "repo": t.slug,                       # owner/name
            "base_commit": r.get("pr1_base_sha", ""),     # agent starts here
            "environment_setup_commit": r.get("pr1_base_sha", ""),
            # the agent's task statement
            "problem_statement": repaired_stmt
                                  or j.get("generated_problem_statement")
                                  or b.get("pr1_problem_statement", ""),
            # original PR1 title and body
            "problem_statement_raw": b.get("pr1_problem_statement", ""),
            "patch": b.get("pr1_patch", ""),              # gold PR1 code
            "test_patch": b.get("pr1_test_patch", ""),    # PR1 test diff
            # validator's PR1 F2P, else recomputed
            "FAIL_TO_PASS": json.dumps(
                fmt_tests(r.get("pr1_f2p_tests"))
                or pr1_tests.get(iid, {}).get("f2p", [])),
            "PASS_TO_PASS": json.dumps(pr1_tests.get(iid, {}).get("p2p", [])),
            # ---- anticipation (held-out PR2) ----
            "anticipation_patch": b.get("pr2_patch", ""),         # gold PR2 code
            "anticipation_test_patch": b.get("pr2_test_patch", ""),
            "ANTICIPATION_FAIL_TO_PASS": json.dumps(fmt_tests(r.get("anticipation_tests"))),
            # ---- PR2-direct task ----
            "pr2_problem_statement": (pr2_stmt.get(iid) or {}).get("pr2_problem_statement", ""),
            "pr2_problem_statement_raw": (pr2_stmt.get(iid) or {}).get("pr2_problem_statement_raw", ""),
            "pr2_spec_repaired": bool((pr2_stmt.get(iid) or {}).get("pr2_spec_repaired", False)),
            "pr2_spec_code": (pr2_code.get(iid) or {}).get("code", ""),  # A = solvable
            "pr1_head_sha": r.get("pr1_head_sha", ""),
            "pr2_base_sha": r.get("pr2_base_sha", ""),
            "pr2_head_sha": r.get("pr2_head_sha", ""),
            "pr1_test_files": json.dumps(r.get("pr1_test_files", [])),
            "anticipation_test_files": json.dumps(
                [f for f in r.get("test_files", [])]),
            # ---- environment ----
            "docker_image": image,
            "repo_path": "/testbed",              # repo location
            "language": "python",
            "python_version": getattr(t, "python", "3.12"),
            "install_args": json.dumps(list(t.install_args)),
            "extra_deps": " ".join(extra),
            "addopts_clear": addopts_clear,
            "runner": getattr(t, "runner", "pytest"),
            "install_cmd": f"uv venv --python {getattr(t,'python','3.12')} .venv && "
                           f"uv pip install --python .venv/bin/python -e . {' '.join(extra)}".strip(),
            "test_cmd": (
                f".venv/bin/python tests/runtests.py --settings={getattr(t,'django_settings','test_sqlite')} "
                f"--parallel 1 --verbosity 2 <test_labels>"
                if is_django else
                f".venv/bin/python -m pytest <test_files> "
                f"--junitxml=/tmp/j.xml -p no:cacheprovider -q{ad}"),
            "run_helper": (
                "run_state_django.sh <sha> <overlay_sha|-> /tmp/j.xml <test_files...>"
                if is_django else
                "run_state.sh <sha> <overlay_sha|-> /tmp/j.xml <test_files...>"),
            # ---- provenance and quality ----
            "earlier_pr": r["earlier_pr"],
            "later_pr": r["later_pr"],
            "mining_category": r.get("category", ""),
            "mining_evidence": r.get("evidence", ""),
            "quality": quality,
            # ---- bug labels ----
            "severity": sev_v.get("severity", ""),
            "failure_mode": sev_verdict.get("failure_mode", ""),   # silent / loud / none
            "trigger_path": sev_verdict.get("trigger_path", ""),   # default / opt-in
            "mechanism": sev_verdict.get("mechanism", ""),         # e.g. regression
            "difficulty": meta.get("difficulty", ""),   # PR2 difficulty
            "pr1_difficulty": (pr1_diff.get(iid) or {}).get("pr1_difficulty") or "",
            "clarity_score": clarity if clarity is not None else "",
            "pr1_code": task_code or "",          # final PR1 code
            # PR1 code before repair
            "pr1_code_original": orig_code or "",
            "pr1_spec_repaired": spec_repaired,
            "created_at": d1,
            "pr2_created_at": d2,
            "gap_days": gap_days(d1, d2),
            "eval_ready": eval_ready,
        }
        # both test sets present
        rec["fully_gradeable"] = bool(json.loads(rec["FAIL_TO_PASS"])
                                      and json.loads(rec["ANTICIPATION_FAIL_TO_PASS"]))
        if args.fully_gradeable and not rec["fully_gradeable"]:
            continue
        records.append(rec)

    OUT_JSONL.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    cols = list(records[0].keys())

    ready = [r for r in records if r["eval_ready"]]
    f2p = sum(1 for r in ready if json.loads(r["FAIL_TO_PASS"]))
    p2p = sum(1 for r in ready if json.loads(r["PASS_TO_PASS"]))
    af2p = sum(1 for r in ready if json.loads(r["ANTICIPATION_FAIL_TO_PASS"]))
    fully = sum(1 for r in ready
                if json.loads(r["FAIL_TO_PASS"]) and json.loads(r["ANTICIPATION_FAIL_TO_PASS"]))
    print(f"wrote {OUT_JSONL.name}: {len(records)} rows ({len(ready)} eval_ready), {len(cols)} fields")
    if n_date_dropped:
        print(f"  SZZ date guard dropped {n_date_dropped} chain(s): blamed introducer "
              f"post-dates the bug report (mined before the in-blame rule landed)")
    print(f"  PR1 FAIL_TO_PASS populated      : {f2p}/{len(ready)}")
    print(f"  PR1 PASS_TO_PASS populated      : {p2p}/{len(ready)}")
    print(f"  ANTICIPATION_FAIL_TO_PASS       : {af2p}/{len(ready)}")
    print(f"  fully gradeable (PR1 F2P + PR2 F2P): {fully}/{len(ready)}")
    if not PR1_TESTS.exists():
        print("  NOTE: data/pr1_tests.json missing — run compute_p2p_image.py to fill PASS_TO_PASS"
              " and recover PR1 FAIL_TO_PASS.")


if __name__ == "__main__":
    main()
