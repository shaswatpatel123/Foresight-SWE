#!/usr/bin/env python3
"""Judge whether PR2 tests are solvable from its statement."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "judges"))

import render_judge_prompts as rjp  # shared judge helpers

DS = ROOT / "data" / "foresight_dataset.jsonl"
PR2 = ROOT / "data" / "pr2_problem_statements.jsonl"
OUT = ROOT / "data" / "judge_pr2_results.jsonl"
PROMPTS = ROOT / "judges" / "prompts" / "swe_rebench_v2"


def _ensure_key() -> None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    tok = ROOT / ".deepseek_token"
    if tok.exists():
        os.environ["DEEPSEEK_API_KEY"] = tok.read_text().strip()
        return
    sys.exit("no DEEPSEEK_API_KEY in env and no .deepseek_token file")


def load_pr2_statements() -> dict[str, str]:
    if not PR2.exists():
        sys.exit(f"missing {PR2} -- run scripts/gen_pr2_problem_statements.py first")
    out = {}
    for line in PR2.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("pr2_problem_statement"):
                out[r["instance_id"]] = r["pr2_problem_statement"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render one meta prompt, no API call")
    ap.add_argument("--limit", type=int, help="judge at most N instances this run")
    ap.add_argument("--cap", type=int, default=60000, help="patch char cap (default 60000)")
    ap.add_argument("--max-tokens", type=int, default=32000)
    args = ap.parse_args()

    templates = {
        "interfaces": (PROMPTS / "interfaces.j2").read_text(),
        "meta_info": (PROMPTS / "meta_info.j2").read_text(),
    }
    pr2_stmt = load_pr2_statements()

    rows = [json.loads(l) for l in DS.read_text().splitlines() if l.strip()]
    rows.sort(key=lambda r: r["instance_id"])
    missing = [r["instance_id"] for r in rows if r["instance_id"] not in pr2_stmt]
    if missing:
        print(f"WARNING: {len(missing)} instances missing a PR2 statement: {missing[:5]}...",
              flush=True)

    done = set()
    if OUT.exists():
        done = {json.loads(l)["instance_id"] for l in OUT.read_text().splitlines() if l.strip()}
    todo = [r for r in rows if r["instance_id"] not in done and r["instance_id"] in pr2_stmt]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(done)} done, {len(todo)} to judge (of {len(rows)})", flush=True)

    def render_meta(r: dict) -> str:
        stmt = pr2_stmt[r["instance_id"]]
        tpatch = rjp.cap(r.get("anticipation_test_patch", ""), args.cap)
        patch = rjp.cap(r.get("anticipation_patch", ""), args.cap)
        iface = rjp.deepseek(rjp.render(
            templates["interfaces"], repo=r["repo"],
            problem_statement=stmt, test_patch=tpatch, patch=patch), args.max_tokens)
        return rjp.render(
            templates["meta_info"], repo=r["repo"],
            problem_statement=stmt, interface=iface, test_patch=tpatch, patch=patch)

    if args.dry_run:
        # no API: placeholder interface
        r = todo[0]
        stmt = pr2_stmt[r["instance_id"]]
        p = rjp.render(
            templates["meta_info"], repo=r["repo"], problem_statement=stmt,
            interface="<interface summary goes here>",
            test_patch=rjp.cap(r.get("anticipation_test_patch", ""), args.cap),
            patch=rjp.cap(r.get("anticipation_patch", ""), args.cap))
        print(f"--- rendered meta prompt for {r['instance_id']} ({len(p)} chars) ---\n"
              f"{p[:3000]}\n... [truncated]")
        return

    _ensure_key()
    with OUT.open("a") as f:
        for i, r in enumerate(todo, 1):
            iid = r["instance_id"]
            out = {"instance_id": iid, "repo": r["repo"], "later_pr": r.get("later_pr")}
            try:
                raw = rjp.deepseek(render_meta(r), args.max_tokens)
                meta = rjp.extract_json(raw)
                out["pr2_meta"] = meta
                code = (meta or {}).get("code")
                issues = (meta or {}).get("detected_issues") or {}
                out["code"] = code
                out["b2"] = bool(issues.get("B2")) or code == "B2"
                out["flagged"] = code is not None and code != "A"
                out["pr2_difficulty"] = (meta or {}).get("difficulty")  # PR2 difficulty
                out["raw"] = raw if meta is None else ""
            except Exception as e:  # record, keep going
                out["error"] = f"{type(e).__name__}: {e}"
            f.write(json.dumps(out) + "\n")
            f.flush()
            tag = out.get("code") or out.get("error", "?")
            print(f"  [{i}/{len(todo)}] {iid:34s} -> {tag}"
                  f"{'  (B2)' if out.get('b2') else ''}", flush=True)

    res = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()]
    codes = Counter(x.get("code") for x in res if "error" not in x)
    n_b2 = sum(1 for x in res if x.get("b2"))
    n_flag = sum(1 for x in res if x.get("flagged"))
    n_err = sum(1 for x in res if x.get("error"))
    cost = rjp._USAGE["in"] * rjp.PRICE_IN + rjp._USAGE["out"] * rjp.PRICE_OUT
    print(f"\n{len(res)} judged | codes={dict(codes)} | B2={n_b2} | flagged(!=A)={n_flag} | "
          f"errors={n_err} | model={rjp.MODEL} | this run ~${cost:.2f}")


if __name__ == "__main__":
    main()
