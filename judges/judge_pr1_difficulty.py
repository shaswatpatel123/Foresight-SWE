#!/usr/bin/env python3
"""Judge PR1 task difficulty."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "judges"))

import render_judge_prompts as rjp  # shared judge helpers

DS = ROOT / "data" / "foresight_dataset.jsonl"
OUT = ROOT / "data" / "pr1_difficulty_results.jsonl"
PROMPTS = ROOT / "judges" / "prompts" / "swe_rebench_v2"


def _ensure_key() -> None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    tok = ROOT / ".deepseek_token"
    if tok.exists():
        os.environ["DEEPSEEK_API_KEY"] = tok.read_text().strip()
        return
    sys.exit("no DEEPSEEK_API_KEY in env and no .deepseek_token file")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render one prompt, no API call")
    ap.add_argument("--limit", type=int, help="judge at most N instances this run")
    ap.add_argument("--cap", type=int, default=60000, help="patch char cap (default 60000)")
    args = ap.parse_args()

    templates = {
        "interfaces": (PROMPTS / "interfaces.j2").read_text(),
        "meta_info": (PROMPTS / "meta_info.j2").read_text(),
    }
    rows = [json.loads(l) for l in DS.read_text().splitlines() if l.strip()]
    rows.sort(key=lambda r: r["instance_id"])

    done = set()
    if OUT.exists():
        done = {json.loads(l)["instance_id"] for l in OUT.read_text().splitlines() if l.strip()}
    todo = [r for r in rows if r["instance_id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(done)} done, {len(todo)} to judge (of {len(rows)})", flush=True)

    if args.dry_run:
        r = todo[0]
        p = rjp.render(templates["meta_info"], repo=r["repo"],
                       problem_statement=r["problem_statement"],
                       interface="<interface summary goes here>",
                       test_patch=rjp.cap(r["test_patch"], args.cap),
                       patch=rjp.cap(r["patch"], args.cap))
        print(f"--- rendered meta prompt for {r['instance_id']} "
              f"({len(p)} chars) ---\n{p[:3000]}\n... [truncated]")
        return

    _ensure_key()
    with OUT.open("a") as f:
        for i, r in enumerate(todo, 1):
            iid = r["instance_id"]
            out = {"instance_id": iid, "spec_repaired": bool(r.get("pr1_spec_repaired"))}
            try:
                iface = rjp.deepseek(rjp.render(
                    templates["interfaces"], repo=r["repo"],
                    problem_statement=r["problem_statement"],
                    test_patch=rjp.cap(r["test_patch"], args.cap),
                    patch=rjp.cap(r["patch"], args.cap)))
                raw = rjp.deepseek(rjp.render(
                    templates["meta_info"], repo=r["repo"],
                    problem_statement=r["problem_statement"],
                    interface=iface,
                    test_patch=rjp.cap(r["test_patch"], args.cap),
                    patch=rjp.cap(r["patch"], args.cap)))
                meta = rjp.extract_json(raw)
                out["pr1_meta"] = meta
                out["pr1_difficulty"] = (meta or {}).get("difficulty")
                out["raw"] = raw if meta is None else ""
            except Exception as e:  # record, keep going
                out["error"] = f"{type(e).__name__}: {e}"
            f.write(json.dumps(out) + "\n")
            f.flush()
            print(f"  [{i}/{len(todo)}] {iid:34s} -> "
                  f"{out.get('pr1_difficulty') or out.get('error', '?')}", flush=True)

    res = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()]
    from collections import Counter
    dist = Counter(x.get("pr1_difficulty") for x in res)
    cost = rjp._USAGE["in"] * rjp.PRICE_IN + rjp._USAGE["out"] * rjp.PRICE_OUT
    print(f"\n{len(res)} judged | distribution: {dict(dist)} | this run ~${cost:.2f}")


if __name__ == "__main__":
    main()
