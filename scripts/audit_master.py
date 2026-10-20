#!/usr/bin/env python3
"""Check master-table invariants, or diff two snapshots."""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config                      # noqa: E402
from foresight import store                       # noqa: E402
from foresight.filters import _instant             # noqa: E402

ROOT = config.REPO_ROOT

# fields whose transitions are listed
OUTCOME_KEYS = ("judge.outcome", "plan.outcome", "install.outcome",
                "validation.outcome", "judge.gradeable", "plan.provenance",
                "validation.status")


# ------------------------------------------------------------------ invariants

def invariants(rows: list[dict]) -> dict[str, list[str]]:
    P: dict[str, list[str]] = collections.defaultdict(list)

    for r in rows:
        i = r["instance_id"]; repo = r["repo"]
        # identity
        if i != f"{repo}__{r['introducer_pr']}-{r['fixer_pr']}":
            P["id_disagrees_with_prs"].append(i)
        for k in ("pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha"):
            if not r.get(k):
                P[f"missing_{k}"].append(i)
        if r["introducer_pr"] == r["fixer_pr"]:
            P["introducer_equals_fixer"].append(i)
        if (r["introducer_pr"] and r["fixer_pr"] and r["introducer_pr"] > r["fixer_pr"]
                and _instant(r.get("pr1_date")) and _instant(r.get("pr2_date"))
                and _instant(r["pr1_date"]) > _instant(r["pr2_date"])):
            # only when dates also invert
            P["pr_number_and_date_both_inverted"].append(i)
        a, b = _instant(r.get("pr1_date")), _instant(r.get("pr2_date"))
        if a and b and a > b:                       # instants, never strings
            P["date_inversion"].append(i)
        if r.get("pr1_head_sha") and r["pr1_head_sha"] == r.get("pr2_head_sha"):
            P["pr1_head_equals_pr2_head"].append(i)
        if not r.get("sources"):
            P["no_source"].append(i)
        try:
            if repo not in config.TARGETS:
                P["repo_not_in_config"].append(i)
            elif r.get("language") != config.TARGETS[repo].language:
                P["language_mismatch"].append(i)
        except Exception:
            pass
        f = r.get("filters") or {}
        j = r.get("judge"); v = r.get("validation")
        # cross-block contradictions
        if j and j.get("gradeable") and f.get("pr2_has_test_changes") is False:
            P["gradeable_but_pr2_has_no_tests"].append(i)
        if j and j.get("gradeable") and (f.get("recency_tier") or 99) <= 5 and not r.get("plan"):
            P["gradeable_in_window_but_unplanned"].append(i)
        if v:
            n_ant = v.get("n_anticipation") or 0
            if v["outcome"] == "ok_full" and n_ant == 0:
                P["validated_but_zero_anticipation"].append(i)
            if (v.get("n_pr1_f2p") or 0) > 500:
                P["implausible_f2p_gt500"].append(i)
            if (v.get("n_pr1_p2p") or 0) > 20000:
                P["implausible_p2p_gt20k"].append(i)
            oc = v.get("outcomes") or {}
            if v["outcome"] == "ok_full" and any(n == 0 for n in oc.values()):
                P["ok_but_a_state_collected_nothing"].append(i)
            if v.get("status") == "validated" and v.get("run_id") != "v1-local" \
                    and not r.get("install", {}).get("pr1_base"):
                P["validated_without_a_recipe"].append(i)
        if r.get("install", {}).get("pr1_base", {}) and r["install"]["pr1_base"].get("malformed"):
            P["malformed_recipe"].append(i)

    # duplicate endpoints under different ids
    seen = collections.defaultdict(list)
    for r in rows:
        if r.get("pr1_head_sha") and r.get("pr2_head_sha"):
            seen[(r["repo"], r["pr1_head_sha"], r["pr2_head_sha"])].append(r["instance_id"])
    for k, v in seen.items():
        if len(v) > 1:
            P["same_endpoints_different_id"].append(",".join(sorted(v)))
    return dict(P)


# ------------------------------------------------------------------------ diff

def _flat(row: dict) -> dict[str, str]:
    """Row as `{field or block.key: json}`."""
    out: dict[str, str] = {}
    for k, v in row.items():
        if k in store.BLOCKS and isinstance(v, dict):
            for sk, sv in v.items():
                out[f"{k}.{sk}"] = json.dumps(sv, sort_keys=True)
        elif k in store.BLOCKS and v is None:
            # absent block: emit nothing
            continue
        else:
            out[k] = json.dumps(v, sort_keys=True)
    return out


def diff(old: list[dict], new: list[dict]) -> dict:
    o = {r["instance_id"]: r for r in old}
    n = {r["instance_id"]: r for r in new}
    added, removed, common = sorted(set(n) - set(o)), sorted(set(o) - set(n)), set(o) & set(n)

    changed: dict[str, list[str]] = collections.defaultdict(list)
    transitions: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for rid in common:
        fo, fn = _flat(o[rid]), _flat(n[rid])
        for key in set(fo) | set(fn):
            a, b = fo.get(key), fn.get(key)
            if a != b:
                changed[key].append(rid)
                if key in OUTCOME_KEYS:
                    transitions[key][f"{json.loads(a) if a else None} -> "
                                     f"{json.loads(b) if b else None}"] += 1

    blocks = collections.Counter(k.split(".")[0] for k in changed)
    return {
        "rows_before": len(o), "rows_after": len(n),
        "added": added, "removed": removed,
        "changed_fields": {k: v for k, v in sorted(changed.items(), key=lambda kv: -len(kv[1]))},
        "changed_blocks": dict(blocks),
        "transitions": {k: dict(v) for k, v in transitions.items()},
    }


def _report_diff(d: dict, expect_block: str | None) -> int:
    print(f"rows: {d['rows_before']} -> {d['rows_after']}"
          f"  (+{len(d['added'])} / -{len(d['removed'])})\n")
    if d["added"]:
        print(f"  added   {len(d['added']):6d}   e.g. {d['added'][:3]}")
    if d["removed"]:
        print(f"  removed {len(d['removed']):6d}   e.g. {d['removed'][:3]}")
    if not d["changed_fields"]:
        print("  no field changed.")
    for key, ids in d["changed_fields"].items():
        print(f"  {key:34s} {len(ids):6d}   e.g. {ids[0]}")
        for move, n in sorted(d["transitions"].get(key, {}).items(), key=lambda kv: -kv[1]):
            print(f"      {move:56s} {n:6d}")

    if expect_block is None:
        return 0
    touched = set(d["changed_blocks"])
    stray = sorted(touched - {expect_block})
    if stray or d["removed"]:
        print(f"\nFAIL: declared owner is {expect_block!r} but "
              f"{'rows were removed and ' if d['removed'] else ''}"
              f"these also moved: {stray or '-'}")
        return 1
    print(f"\nOK: only {expect_block!r} moved.")
    return 0


# ------------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"),
                    help="compare two master snapshots instead of auditing invariants")
    ap.add_argument("--expect-block", default=None,
                    help="with --diff: the one block the writing stage owns; exit 1 "
                         "if anything else moved")
    ap.add_argument("--json", default="", help="also write the report here")
    args = ap.parse_args()

    if args.diff:
        d = diff(store.load(args.diff[0]), store.load(args.diff[1]))
        rc = _report_diff(d, args.expect_block)
        if args.json:
            Path(args.json).write_text(json.dumps(d, indent=1))
            print(f"-> {args.json}")
        sys.exit(rc)

    rows = store.load()
    P = invariants(rows)
    print(f"rows audited: {len(rows)}\n")
    for k in sorted(P, key=lambda k: -len(P[k])):
        print(f"  {k:38s} {len(P[k]):6d}   e.g. {P[k][0][:70]}")
    out = Path(args.json) if args.json else ROOT / "data" / "master_audit.json"
    out.write_text(json.dumps(P, indent=1))
    print(f"\n-> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
