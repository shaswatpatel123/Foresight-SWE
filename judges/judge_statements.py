#!/usr/bin/env python3
"""Check PR1 statements don't leak the answer."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "judges"))
import render_judge_prompts as rjp

DS = ROOT / "data" / "foresight_dataset.jsonl"
PROMPT = (Path(__file__).resolve().parent / "prompts" / "statement_fairness.j2").read_text()
OUT = ROOT / "data" / "statement_fairness_results.jsonl"


def judge_one(r: dict) -> dict:
    iid = r["instance_id"]
    out = {"instance_id": iid, "repo": r["repo"]}
    try:
        prompt = rjp.render(
            PROMPT, repo=r["repo"],
            pr1_statement=rjp.cap(r["problem_statement"], 6000),
            pr1_patch=rjp.cap(r["patch"], 12000),
            anticipation_patch=rjp.cap(r["anticipation_patch"], 10000),
            anticipation_test_patch=rjp.cap(r["anticipation_test_patch"], 10000),
        )
        v = rjp.extract_json(rjp.deepseek(prompt)) or {}
        out["verdict"] = v.get("verdict")
        out["evidence"] = v.get("evidence")
        out["reasoning"] = v.get("reasoning")
        out["repair_hint"] = v.get("repair_hint")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        tok = ROOT / ".deepseek_token"
        if not tok.exists():
            sys.exit("no .deepseek_token")
        os.environ["DEEPSEEK_API_KEY"] = tok.read_text().strip()

    rows = [json.loads(l) for l in DS.read_text().splitlines() if l.strip()]
    clean = [r for r in rows if r.get("quality") == "clean"]
    done = set()
    if OUT.exists():
        done = {json.loads(l)["instance_id"] for l in OUT.read_text().splitlines() if l.strip()}
    todo = [r for r in clean if r["instance_id"] not in done]
    print(f"{len(clean)} clean instances; {len(done)} done, {len(todo)} to judge", flush=True)

    with OUT.open("a") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(judge_one, r) for r in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            o = fut.result()
            f.write(json.dumps(o) + "\n"); f.flush()
            print(f"  [{i}/{len(todo)}] {o['instance_id']:26s} {o.get('verdict') or o.get('error')}", flush=True)

    res = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()]
    res = [r for r in res if r.get("verdict")]
    print(f"\n===== {len(res)} judged =====")
    print("verdicts:", dict(Counter(r["verdict"] for r in res)))
    flagged = [r for r in res if r["verdict"] != "clean"]
    print(f"\n{len(flagged)} NOT clean (fairness concerns):")
    for r in sorted(flagged, key=lambda x: x["verdict"]):
        print(f"\n  {r['instance_id']} [{r['verdict']}]")
        print(f"    evidence: {r.get('evidence')}")
        print(f"    why: {(r.get('reasoning') or '')[:240]}")


if __name__ == "__main__":
    main()
