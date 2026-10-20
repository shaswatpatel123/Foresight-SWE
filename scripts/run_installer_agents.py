#!/usr/bin/env python3
"""Run the installer agent over all commits, in parallel."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config

ROOT = config.REPO_ROOT
MANIFEST = ROOT / "docker" / "validate_instances.json"
OUT_DIR = ROOT / "docker" / "install"
RUNS = config.DATA_DIR / "installer_runs"
SINGLE = ROOT / "scripts" / "run_installer_agent.py"


def build_worklist(rows: list[dict], repos: set[str], eras: tuple[str, ...],
                   force: bool) -> list[dict]:
    """(repo, commit) pairs still needing a recipe."""
    seen: set[tuple[str, str]] = set()
    work = []
    for m in rows:
        if repos and m["repo"] not in repos:
            continue
        for era in eras:
            sha = m["pr1_base_sha"] if era == "pr1" else m["pr2_head_sha"]
            if not sha:
                continue
            key = (m["repo"], sha[:12])
            if key in seen:
                continue
            seen.add(key)
            recipe = OUT_DIR / m["repo"] / f"{sha[:12]}.json"
            if recipe.exists() and not force:
                continue
            work.append({"instance_id": m["instance_id"], "repo": m["repo"],
                         "era": era, "sha12": sha[:12]})
    return work


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="", help="comma list; default all")
    ap.add_argument("--manifest", default=str(MANIFEST),
                    help="per-LANGUAGE manifest to drive (see run_installer_agent.py)")
    ap.add_argument("--skip-repo", default="", help="comma list to exclude")
    ap.add_argument("--era", default="", choices=["", "pr1", "pr2"])
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--api-base", default="http://localhost:8021/v1")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--max-steps", type=int, default=100,
                    help="ceiling, not a target: a run stops two observations after the tests execute, so only genuinely hard builds get near this. The model is a local vLLM, so steps are cheap")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--force", action="store_true",
                    help="re-run even where a recipe already exists")
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()

    rows = json.loads(Path(args.manifest).read_text())
    repos = {s.strip() for s in args.repo.split(",") if s.strip()}
    skip = {s.strip() for s in args.skip_repo.split(",") if s.strip()}
    if skip:
        rows = [m for m in rows if m["repo"] not in skip]
    eras = (args.era,) if args.era else ("pr1", "pr2")
    work = build_worklist(rows, repos, eras, args.force)
    if args.limit:
        work = work[: args.limit]

    total_slots = sum(1 for m in rows for e in eras
                      if (not repos or m["repo"] in repos))
    if not work:
        print(f"nothing to do: every commit in scope already has a recipe "
              f"({total_slots} slots)")
        return

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    print(f"installer agent: {len(work)} distinct (repo, commit) to configure "
          f"out of {total_slots} (chain, era) slots -- the rest are shared commits "
          f"or already done")
    print(f"model={args.model} api={args.api_base} workers={args.workers}")
    print(f"recipes -> docker/install/<repo>/  logs -> {RUNS / run_id}")

    def sweep(w: dict) -> int:
        """Remove this item's leftover container."""
        prefix = f"installer_{w['instance_id'].replace('__', '_')}_{w['era']}_"
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{prefix}"],
                             capture_output=True, text=True).stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
        return len(ids)

    def one(w: dict) -> dict:
        cmd = [sys.executable, str(SINGLE), "--instance", w["instance_id"],
               "--manifest", args.manifest,
               "--era", w["era"], "--platform", args.platform,
               "--api-base", args.api_base, "--model", args.model,
               "--max-steps", str(args.max_steps), "--run-id", run_id]
        t0 = time.time()
        leaked = 0
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               errors="replace", timeout=args.timeout)
            rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            rc, out = 124, "TIMEOUT"
        finally:
            leaked = sweep(w)
        return {**w, "rc": rc, "seconds": round(time.time() - t0),
                "accepted": rc == 0, "leaked_container": leaked,
                "tail": out.strip().splitlines()[-1][:200] if out.strip() else ""}

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, w): w for w in work}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            flag = "ok  " if r["accepted"] else "FAIL"
            print(f"  [{i}/{len(work)}] {flag} {r['repo']:12s} {r['sha12']} "
                  f"{r['era']} {r['seconds']:5d}s  {r['tail']}", flush=True)

    outp = RUNS / run_id / "agents.jsonl"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text("".join(json.dumps(r) + "\n" for r in results))
    ok = sum(1 for r in results if r["accepted"])
    print(f"\naccepted {ok}/{len(results)}  -> {outp}")
    print("commits with no recipe will surface as env_failed in validation, "
          "which is the honest outcome -- not a crash")


if __name__ == "__main__":
    main()
