#!/usr/bin/env python3
"""Write PR2 problem statements for the PR2-direct eval."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "judges"))

import render_judge_prompts as rjp  # shared judge helpers

DS = ROOT / "data" / "foresight_dataset.jsonl"
OUT = ROOT / "data" / "pr2_problem_statements.jsonl"
PROMPTS = ROOT / "judges" / "prompts" / "swe_rebench_v2"

# bundles with the raw PR2 text
_BUNDLE_GLOBS = ["data/judge_bundle_*.jsonl"]
_BUNDLE_FILES = ["data/_bundle_75.jsonl", "data/judge_poc.jsonl"]


def _ensure_key() -> None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    tok = ROOT / ".deepseek_token"
    if tok.exists():
        os.environ["DEEPSEEK_API_KEY"] = tok.read_text().strip()
        return
    sys.exit("no DEEPSEEK_API_KEY in env and no .deepseek_token file")


def load_raw_pr2() -> dict[str, str]:
    """instance_id -> raw PR2 description (title+body), merged across bundles."""
    paths = []
    for g in _BUNDLE_GLOBS:
        paths += glob.glob(str(ROOT / g))
    paths += [str(ROOT / f) for f in _BUNDLE_FILES]
    raw: dict[str, str] = {}
    for p in paths:
        if not Path(p).exists():
            continue
        for line in Path(p).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            iid, text = r.get("instance_id"), r.get("pr2_problem_statement")
            if iid and text:
                raw.setdefault(iid, text)  # first non-empty wins
    return raw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render one prompt, no API call")
    ap.add_argument("--limit", type=int, help="generate at most N this run")
    ap.add_argument("--cap", type=int, default=60000, help="patch char cap (default 60000)")
    ap.add_argument("--max-tokens", type=int, default=32000)
    args = ap.parse_args()

    template = (PROMPTS / "pr_description.j2").read_text()
    raw_pr2 = load_raw_pr2()

    rows = [json.loads(l) for l in DS.read_text().splitlines() if l.strip()]
    rows.sort(key=lambda r: r["instance_id"])

    missing = [r["instance_id"] for r in rows if r["instance_id"] not in raw_pr2]
    if missing:
        print(f"WARNING: {len(missing)} instances missing raw PR2 text: {missing[:5]}...",
              flush=True)

    done = set()
    if OUT.exists():
        done = {json.loads(l)["instance_id"] for l in OUT.read_text().splitlines() if l.strip()}
    todo = [r for r in rows if r["instance_id"] not in done and r["instance_id"] in raw_pr2]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(done)} done, {len(todo)} to generate (of {len(rows)})", flush=True)

    def render_one(r: dict) -> str:
        # same as the PR1 statement pass
        return rjp.render(
            template,
            repo=r["repo"],
            pr_description=raw_pr2[r["instance_id"]],
            patch=rjp.cap(r.get("anticipation_patch") or r.get("anticipation_test_patch", ""),
                          args.cap),
        )

    if args.dry_run:
        r = todo[0]
        p = render_one(r)
        print(f"--- rendered pr_description prompt for {r['instance_id']} "
              f"({len(p)} chars) ---\n{p[:3000]}\n... [truncated]")
        return

    _ensure_key()
    with OUT.open("a") as f:
        for i, r in enumerate(todo, 1):
            iid = r["instance_id"]
            out = {"instance_id": iid, "repo": r["repo"], "later_pr": r.get("later_pr")}
            try:
                out["pr2_problem_statement"] = rjp.deepseek(render_one(r), args.max_tokens).strip()
                out["pr2_problem_statement_raw"] = raw_pr2[iid]
            except Exception as e:  # record, keep going
                out["error"] = f"{type(e).__name__}: {e}"
            f.write(json.dumps(out) + "\n")
            f.flush()
            status = "ok" if "pr2_problem_statement" in out else out.get("error", "?")
            print(f"  [{i}/{len(todo)}] {iid:34s} -> {status}", flush=True)

    res = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()]
    ok = sum(1 for x in res if x.get("pr2_problem_statement"))
    cost = rjp._USAGE["in"] * rjp.PRICE_IN + rjp._USAGE["out"] * rjp.PRICE_OUT
    print(f"\n{len(res)} written ({ok} ok) | model={rjp.MODEL} | this run ~${cost:.2f}")


if __name__ == "__main__":
    main()
