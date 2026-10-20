#!/usr/bin/env python3
"""Run the stage-1a screens into the `filters` block."""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import filters, store  # noqa: E402

GIT_KEYS = [k for k in filters.KEYS if k != "recency_tier"]


def _unscreened(row: dict) -> bool:
    f = row.get("filters") or {}
    return all(f.get(k) is None for k in GIT_KEYS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8,
                    help="git-diff concurrency; blame-free, so the output is "
                         "identical at any width")
    ap.add_argument("--repos", default="", help="restrict to these repos (comma list)")
    ap.add_argument("--force", action="store_true",
                    help="rescreen chains that already carry filters")
    ap.add_argument("--limit", type=int, default=0, help="screen at most N chains")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, but leave the master untouched")
    ap.add_argument("--store", default="",
                    help="chain file to screen; defaults to the master. The store "
                         "layer has always been path-parameterised -- this exposes "
                         "it so a side file (e.g. the security stratum) gets the "
                         "SAME screens without a second implementation, and without "
                         "writing the master while another process owns it.")
    args = ap.parse_args()

    path = pathlib.Path(args.store) if args.store else store.MASTER
    rows = store.load(path)
    todo = rows if args.force else [r for r in rows if _unscreened(r)]
    if args.repos:
        want = {r.strip() for r in args.repos.split(",") if r.strip()}
        todo = [r for r in todo if r["repo"] in want]
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(rows)} chains in the master, {len(todo)} to screen", flush=True)
    if not todo:
        return

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        computed = list(pool.map(filters.compute, todo))

    updates = {r["instance_id"]: f for r, f in zip(todo, computed)}

    # blank = clone or SHA missing
    n_blank = sum(1 for f in computed if all(f[k] is None for k in GIT_KEYS))
    changed_from_real = [
        r["instance_id"] for r, f in zip(todo, computed)
        if not _unscreened(r) and {k: (r["filters"] or {}).get(k) for k in filters.KEYS} != f]

    if args.dry_run:
        print(f"dry run: {len(updates)} would be written, "
              f"{len(changed_from_real)} would CHANGE an existing answer")
        for i in changed_from_real[:5]:
            print(f"  {i}")
    else:
        report = store.update("filters", updates, path=path)
        print(report)

    ok = [f for f in computed if f["pr2_has_test_changes"] is not None]
    has = [f for f in ok if f["pr2_has_test_changes"]]
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r, f in zip(todo, computed):
        if f["pr2_has_test_changes"] is not None:
            by[r.get("language") or "?"][0] += 1
            by[r.get("language") or "?"][1] += int(f["pr2_has_test_changes"])

    print(f"\n===== STAGE 1a: {len(ok)} screened, {n_blank} unscreenable "
          f"(clone missing or SHA not present) =====")
    print(f"pr2_has_test_changes: {len(has)}/{len(ok)} = "
          f"{len(has) / max(len(ok), 1) * 100:.0f}% -> eligible for the judge; "
          f"{len(ok) - len(has)} screened out (recorded, not dropped)")
    for lang, (tot, h) in sorted(by.items()):
        print(f"  {lang:11s} {h}/{tot} have PR2 test changes")
    tiers = Counter(f["recency_tier"] for f in computed)
    print("  recency tiers: " + ", ".join(
        f"{t}:{n}" for t, n in sorted(tiers.items(), key=lambda kv: (kv[0] is None, kv[0]))))
    if changed_from_real:
        print(f"\nNOTE: {len(changed_from_real)} chain(s) changed an answer that was "
              f"already recorded -- e.g. {changed_from_real[:3]}")


if __name__ == "__main__":
    main()
