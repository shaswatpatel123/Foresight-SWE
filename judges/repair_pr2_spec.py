#!/usr/bin/env python3
"""Repair PR2 statements the judge flagged."""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "judges"))
import render_judge_prompts as J  # shared judge helpers

PROMPTS = ROOT / "judges" / "prompts" / "swe_rebench_v2"
DS = ROOT / "data" / "foresight_dataset.jsonl"
PR2 = ROOT / "data" / "pr2_problem_statements.jsonl"
JUDGE = ROOT / "data" / "judge_pr2_results.jsonl"
OUT = ROOT / "data" / "spec_repair_pr2_results.jsonl"
CAP = 30000


def _ensure_key() -> None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    tok = ROOT / ".deepseek_token"
    if tok.exists():
        os.environ["DEEPSEEK_API_KEY"] = tok.read_text().strip()
        return
    sys.exit("no DEEPSEEK_API_KEY in env and no .deepseek_token file")


def load_jsonl(path: Path) -> dict:
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
    return "\n\n".join(bits) or "The held-out tests assert specifics not stated in the description."


def judge_code(repo: str, stmt: str, patch: str, tpatch: str, tmpl: dict) -> str | None:
    """Re-judge the repaired statement."""
    iface = J.deepseek(J.render(tmpl["interfaces"], repo=repo, problem_statement=stmt,
                                test_patch=J.cap(tpatch, CAP), patch=J.cap(patch, CAP)))
    raw = J.deepseek(J.render(tmpl["meta_info"], repo=repo, problem_statement=stmt,
                              interface=iface, test_patch=J.cap(tpatch, CAP),
                              patch=J.cap(patch, CAP)))
    return (J.extract_json(raw) or {}).get("code")


def repair_one(iid: str, repo: str, patch: str, tpatch: str, cur_stmt: str,
               meta: dict, tmpl: dict, max_rounds: int) -> dict:
    out = {"instance_id": iid, "repo": repo, "orig_code": meta.get("code"),
           "new_code": meta.get("code"), "kept": False,
           "repaired_pr2_problem_statement": "", "rounds": 0}
    stmt = cur_stmt
    gap = gap_text(meta)
    try:
        for rnd in range(1, max_rounds + 1):
            repaired = J.deepseek(J.render(tmpl["repair"], repo=repo, pr_description=stmt,
                                           patch=J.cap(patch, CAP), test_patch=J.cap(tpatch, CAP),
                                           gap=gap))
            code = judge_code(repo, repaired, patch, tpatch, tmpl)
            out.update(new_code=code, repaired_pr2_problem_statement=repaired, rounds=rnd)
            if code == "A":
                out["kept"] = True
                break
            stmt = repaired  # retry on the repaired one
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def apply_to_pr2_file(results: list[dict]) -> int:
    """Overwrite pr2_problem_statement with the repaired text for kept instances."""
    rep = {r["instance_id"]: r for r in results if r["kept"] and r["repaired_pr2_problem_statement"]}
    if not rep:
        return 0
    rows = [json.loads(l) for l in PR2.read_text().splitlines() if l.strip()]
    n = 0
    for row in rows:
        r = rep.get(row["instance_id"])
        if r:
            row.setdefault("pr2_problem_statement_prerepair", row["pr2_problem_statement"])
            row["pr2_problem_statement"] = r["repaired_pr2_problem_statement"]
            row["pr2_spec_repaired"] = True
            n += 1
    PR2.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render one repair prompt, no API")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-rounds", type=int, default=2)
    ap.add_argument("--limit", type=int, help="repair at most N flagged instances")
    ap.add_argument("--apply", action="store_true",
                    help="write repaired statements into data/pr2_problem_statements.jsonl")
    args = ap.parse_args()

    tmpl = {n: J.load_template(f"{n}.j2") for n in ("interfaces", "meta_info")}
    tmpl["repair"] = J.load_template("pr_description_repair.j2")

    ds = load_jsonl(DS)
    pr2 = load_jsonl(PR2)
    judge = load_jsonl(JUDGE)

    flagged = [iid for iid, j in judge.items()
               if j.get("flagged") or (j.get("code") and j["code"] != "A")]
    flagged.sort()

    done = set(load_jsonl(OUT)) if OUT.exists() else set()
    todo = [iid for iid in flagged if iid not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(flagged)} flagged | {len(done)} already repaired | {len(todo)} to repair "
          f"(max {args.max_rounds} rounds each)", flush=True)

    if args.dry_run:
        iid = todo[0]
        meta = judge[iid].get("pr2_meta") or {}
        p = J.render(tmpl["repair"], repo=ds[iid]["repo"],
                     pr_description=pr2[iid]["pr2_problem_statement"],
                     patch=J.cap(ds[iid].get("anticipation_patch", ""), CAP),
                     test_patch=J.cap(ds[iid].get("anticipation_test_patch", ""), CAP),
                     gap=gap_text(meta))
        print(f"--- repair prompt for {iid} ({len(p)} chars) ---\n{p[:3500]}\n... [truncated]")
        return

    _ensure_key()
    results = list(load_jsonl(OUT).values()) if OUT.exists() else []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for iid in todo:
            meta = judge[iid].get("pr2_meta") or {}
            r = ds[iid]
            futs[ex.submit(repair_one, iid, r["repo"], r.get("anticipation_patch", ""),
                           r.get("anticipation_test_patch", ""),
                           pr2[iid]["pr2_problem_statement"], meta, tmpl, args.max_rounds)] = iid
        for k, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            results.append(res)
            tag = "KEEP" if res["kept"] else "stay"
            print(f"  [{k}/{len(todo)}] {res['instance_id']:34} {res['orig_code']}->"
                  f"{res.get('new_code')} [{tag}] r{res['rounds']}"
                  f"{'  ERR '+res['error'] if res.get('error') else ''}", flush=True)

    results.sort(key=lambda r: r["instance_id"])
    OUT.write_text("\n".join(json.dumps(r) for r in results) + "\n")
    kept = sum(1 for r in results if r["kept"])
    print(f"\nwrote {OUT}: {len(results)} repaired, {kept} flipped to A, "
          f"{len(results) - kept} still flagged")

    if args.apply:
        n = apply_to_pr2_file(results)
        print(f"applied {n} repaired statements into {PR2.name} "
              f"(originals kept as pr2_problem_statement_prerepair)")

    cost = J._USAGE["in"] * J.PRICE_IN + J._USAGE["out"] * J.PRICE_OUT
    print(f"usage: {J._USAGE.get('calls', '?')} calls | est cost ${cost:.4f}")


if __name__ == "__main__":
    main()
