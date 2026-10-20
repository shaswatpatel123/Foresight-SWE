#!/usr/bin/env python3
"""Judge the severity of the bug PR2 fixed."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "judges"))

import render_judge_prompts as rjp  # shared judge helpers

DS = ROOT / "data" / "foresight_dataset.jsonl"
PROMPT = ROOT / "judges" / "prompts" / "severity_judge.j2"
BUNDLES = [ROOT / "data" / f for f in (
    "judge_bundle_nv.jsonl", "judge_bundle_pandas.jsonl",
    "judge_bundle_django.jsonl", "judge_poc.jsonl")]

_RELNOTE = re.compile(
    r"(^|/)(changelog|changes|news|releases?|whatsnew)(/|\.)|\.(bugfix|feature)(\.rst)?$", re.I)


def changelog_lines(patch: str) -> list[str]:
    """Added lines in changelog/release-note files inside the fix diff."""
    out, in_relnote = [], False
    for ln in (patch or "").splitlines():
        if ln.startswith("+++ "):
            in_relnote = bool(_RELNOTE.search(ln[6:]))
        elif in_relnote and ln.startswith("+") and not ln.startswith("+++"):
            text = ln[1:].strip()
            if text:
                out.append(text)
    return out


def evidence_block(r: dict) -> str:
    parts = [f"- PR1 was merged {r.get('gap_days', '?')} days before PR2 fixed the bug."]
    tests = json.loads(r["ANTICIPATION_FAIL_TO_PASS"])
    parts.append(f"- Held-out tests added by PR2 ({len(tests)}): " + "; ".join(tests[:6])
                 + (" ..." if len(tests) > 6 else ""))
    notes = changelog_lines(r["anticipation_patch"])
    if notes:
        parts.append("- Changelog/release-note text the fix itself added "
                     "(the maintainers' own wording):")
        parts += [f"    {n}" for n in notes[:8]]
    else:
        parts.append("- The fix added no changelog/release-note entry.")
    return "\n".join(parts)


def load_bundles() -> dict:
    merged: dict = {}
    for p in BUNDLES:
        if not p.exists():
            continue
        for l in p.read_text().splitlines():
            if l.strip():
                d = json.loads(l)
                merged[d["instance_id"]] = d
    return merged


def build_prompt(r: dict, bundles: dict, template: str, capn: int) -> str:
    b = bundles.get(r["instance_id"], {})
    pr2_desc = (b.get("pr2_problem_statement") or b.get("pr2_title")
                or "(no human PR2 description available)")
    return rjp.render(
        template,
        repo=r["repo"],
        pr1_task=rjp.cap(r["problem_statement"], 4000),
        pr2_description=rjp.cap(pr2_desc, 4000),
        fix_patch=rjp.cap(r["anticipation_patch"], capn),
        test_patch=rjp.cap(r["anticipation_test_patch"], capn),
        evidence=evidence_block(r),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="instance ids (default: all)")
    ap.add_argument("--dry-run", nargs="?", const="FIRST", metavar="IID",
                    help="render one prompt (optionally for IID), no API call")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cap", type=int, default=40000, help="patch char cap")
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent API calls (default 6)")
    args = ap.parse_args()

    template = PROMPT.read_text()
    rows = [json.loads(l) for l in DS.read_text().splitlines() if l.strip()]
    rows.sort(key=lambda r: r["instance_id"])
    if args.ids:
        want = set(args.ids)
        rows = [r for r in rows if r["instance_id"] in want]
        missing = want - {r["instance_id"] for r in rows}
        if missing:
            sys.exit(f"unknown instance ids: {sorted(missing)}")
    bundles = load_bundles()

    if args.dry_run:
        r = rows[0] if args.dry_run == "FIRST" else \
            next(x for x in rows if x["instance_id"] == args.dry_run)
        p = build_prompt(r, bundles, template, args.cap)
        missing = [m.group(0) for m in re.finditer(r"\{\{\s*\w+\s*\}\}", p)]
        print(f"--- {r['instance_id']} ({len(p)} chars; unfilled vars: {missing or 'none'}) ---")
        print(p)
        return

    model = os.environ.get("DEEPSEEK_MODEL", rjp.MODEL)
    out_path = ROOT / "data" / f"severity_judge_{model.replace('/', '_')}.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(l)["instance_id"] for l in out_path.read_text().splitlines()
                if l.strip()}
    todo = [r for r in rows if r["instance_id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"model={model}  {len(done)} done, {len(todo)} to judge -> {out_path.name}",
          flush=True)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        tok = ROOT / ".deepseek_token"
        if tok.exists():
            os.environ["DEEPSEEK_API_KEY"] = tok.read_text().strip()
        else:
            sys.exit("no DEEPSEEK_API_KEY in env and no .deepseek_token file")

    def judge_one(r: dict) -> dict:
        out = {"instance_id": r["instance_id"], "model": model}
        try:
            raw = rjp.deepseek(build_prompt(r, bundles, template, args.cap))
            verdict = rjp.extract_json(raw)
            out["verdict"] = verdict
            out["severity"] = (verdict or {}).get("severity")
            out["raw"] = raw if verdict is None else ""
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    # calls in a pool, writes here
    with out_path.open("a") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(judge_one, r) for r in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            out = fut.result()
            f.write(json.dumps(out) + "\n")
            f.flush()
            print(f"  [{i}/{len(todo)}] {out['instance_id']:34s} -> "
                  f"{out.get('severity') or out.get('error', '?')}", flush=True)

    res = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    from collections import Counter
    print(f"\n{len(res)} judged | {dict(Counter(x.get('severity') for x in res))}")
    cost = rjp._USAGE["in"] * rjp.PRICE_IN + rjp._USAGE["out"] * rjp.PRICE_OUT
    print(f"this run ~${cost:.2f}")


if __name__ == "__main__":
    main()
