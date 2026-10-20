#!/usr/bin/env python3
"""Add stage-1a screen fields to the pool (additive)."""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config

POOL = config.CHAINS_DIR / "candidate_pool.jsonl"
STAGE1A = config.CHAINS_DIR / "pool_stage1a.jsonl"

FIELDS = ("pr2_has_test_changes", "pr2_n_test_files", "pr2_n_src_files",
          "pr1_has_test_changes", "pr1_n_test_files", "pr1_n_src_files")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pool", default=str(POOL))
    ap.add_argument("--stage1a", default=str(STAGE1A))
    args = ap.parse_args()

    screen = {}
    for line in Path(args.stage1a).read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            screen[r["instance_id"]] = r

    rows = [json.loads(l) for l in Path(args.pool).read_text().splitlines() if l.strip()]
    stats = collections.Counter()
    out = []
    for row in rows:
        s = screen.get(row["instance_id"])
        if not s or s.get("error"):
            stats["no screen row"] += 1
            out.append(row)
            continue
        added = 0
        for f in FIELDS:
            if f in s and f not in row:          # ADD only -- never overwrite
                row[f] = s[f]
                added += 1
        stats["annotated" if added else "already had the fields"] += 1
        out.append(row)

    # what the new fields would gate
    have1 = sum(1 for r in out if r.get("pr1_has_test_changes") is True)
    no1 = sum(1 for r in out if r.get("pr1_has_test_changes") is False)
    have2 = sum(1 for r in out if r.get("pr2_has_test_changes") is True)
    print(f"pool rows: {len(rows)}")
    for k, v in stats.most_common():
        print(f"  {k:24s} {v}")
    print(f"\n  pr2_has_test_changes True : {have2}")
    print(f"  pr1_has_test_changes True : {have1}")
    print(f"  pr1_has_test_changes False: {no1}   <- anticipation-only ceiling, "
          f"never fully gradeable")

    if args.dry_run:
        print("\n(dry run -- pool not written)")
        return
    tmp = Path(args.pool + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in out))
    tmp.replace(Path(args.pool))            # atomic: never a half-written pool
    print(f"\n-> {args.pool} (atomic replace)")


if __name__ == "__main__":
    main()
