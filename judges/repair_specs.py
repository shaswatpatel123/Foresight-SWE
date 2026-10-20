#!/usr/bin/env python3
"""Repair PR1 statements the judge flagged."""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_judge_prompts as J  # shared judge helpers

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "judges" / "prompts" / "swe_rebench_v2"
BUNDLE75 = ROOT / "data" / "_bundle_75.jsonl"
PR1_CODE = ROOT / "data" / "judge_pr1code_results.jsonl"
JUDGE_FILES = ["judge_nv_results.jsonl", "judge_pandas_results.jsonl",
               "judge_django_results.jsonl", "judge_poc_results.jsonl"]  # poc last wins
OUT = ROOT / "data" / "spec_repair_results.jsonl"
CAP = 30000


def load_kv(path: Path) -> dict:
    d = {}
    if path.exists():
        for l in path.read_text().splitlines():
            if l.strip():
                x = json.loads(l)
                d[x["instance_id"]] = x
    return d


def gap_text(meta: dict) -> str:
    bits = []
    if meta.get("reasoning"):
        bits.append("Reviewer reasoning: " + meta["reasoning"])
    issues = meta.get("test_alignment_issues") or []
    if issues:
        bits.append("Specific gaps:\n- " + "\n- ".join(issues))
    code = meta.get("code")
    if code:
        bits.append(f"Detected issue code: {code}")
    return "\n\n".join(bits) or "The tests assert specifics not stated in the description."


def judge_code(repo: str, stmt: str, patch: str, test_patch: str, tmpl: dict) -> str | None:
    """Re-judge the repaired statement."""
    iface = J.deepseek(J.render(tmpl["interfaces"], repo=repo, problem_statement=stmt,
                                test_patch=J.cap(test_patch, CAP), patch=J.cap(patch, CAP)))
    raw = J.deepseek(J.render(tmpl["meta_info"], repo=repo, problem_statement=stmt,
                              interface=iface, test_patch=J.cap(test_patch, CAP),
                              patch=J.cap(patch, CAP)))
    m = J.extract_json(raw)
    return (m or {}).get("code")


def repair_one(rec: dict, meta: dict, cur_stmt: str, tmpl: dict, anchors: list, max_rounds: int) -> dict:
    repo, patch, tpatch = rec["repo"], rec["pr1_patch"], rec["pr1_test_patch"]
    out = {"instance_id": rec["instance_id"], "orig_code": meta.get("code"),
           "new_code": meta.get("code"), "new_clarity": None, "kept": False,
           "repaired_problem_statement": "", "rounds": 0}
    stmt = cur_stmt
    gap = gap_text(meta)
    try:
        for rnd in range(1, max_rounds + 1):
            repaired = J.deepseek(J.render(tmpl["repair"], repo=repo, pr_description=stmt,
                                           patch=J.cap(patch, CAP), test_patch=J.cap(tpatch, CAP),
                                           gap=gap))
            code = judge_code(repo, repaired, patch, tpatch, tmpl)
            clar = J.parse_score(J.deepseek(J.render_clarity(
                tmpl["verified"], repo=repo, problem_statement=repaired, anchors=anchors)))
            out.update(new_code=code, new_clarity=clar, repaired_problem_statement=repaired,
                       rounds=rnd)
            if code == "A" and clar in (0, 1):
                out["kept"] = True
                break
            stmt = repaired  # retry on the repaired one
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-rounds", type=int, default=2)
    args = ap.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("DEEPSEEK_API_KEY not set")

    tmpl = {n: J.load_template(f"{n}.j2") for n in
            ("interfaces", "meta_info", "verified")}
    tmpl["repair"] = J.load_template("pr_description_repair.j2")
    anchors = [e["problem_statement"] for e in
               json.loads((PROMPTS / "clarity_anchors.json").read_text())["examples"]]

    bundle = load_kv(BUNDLE75)
    pr1code = load_kv(PR1_CODE)
    judge = {}
    for f in JUDGE_FILES:
        for k, v in load_kv(ROOT / "data" / f).items():
            judge[k] = v  # poc last -> wins

    # B-flagged PR1 tasks only
    todo = []
    for iid, rec in bundle.items():
        meta = (pr1code.get(iid, {}) or {}).get("pr1_meta") or {}
        if meta.get("code") and meta["code"] != "A":
            cur = judge.get(iid, {}).get("generated_problem_statement") or rec["pr1_problem_statement"]
            todo.append((rec, meta, cur))
    print(f"repairing {len(todo)} B-flagged PR1 tasks (max {args.max_rounds} rounds each)", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(repair_one, r, m, c, tmpl, anchors, args.max_rounds): r["instance_id"]
                for (r, m, c) in todo}
        for k, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            results.append(res)
            tag = "KEEP" if res["kept"] else "drop"
            print(f"  [{k}/{len(todo)}] {res['instance_id']:34} {res['orig_code']}->"
                  f"{res['new_code']} clar={res['new_clarity']} [{tag}]", flush=True)

    results.sort(key=lambda r: r["instance_id"])
    OUT.write_text("\n".join(json.dumps(r) for r in results) + "\n")
    kept = sum(1 for r in results if r["kept"])
    print(f"\nwrote {OUT}: {len(results)} repaired, {kept} flip to A (kept), "
          f"{len(results)-kept} stay flagged")
    cost = J._USAGE["in"] * J.PRICE_IN + J._USAGE["out"] * J.PRICE_OUT
    print(f"usage: {J._USAGE['calls']} calls | est cost ${cost:.4f}")


if __name__ == "__main__":
    main()
