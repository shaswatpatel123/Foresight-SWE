#!/usr/bin/env python3
"""CLI for the Django miner."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight.mining import django_szz


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--out", default="data/chains/django_candidates.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    records, stats = django_szz.mine_chains(
        Path("repos/django"), ref=args.ref, since=args.since, limit=args.limit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    print(f"[mine-django] {stats} -> {out}")


if __name__ == "__main__":
    main()
