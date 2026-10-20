#!/usr/bin/env python3
"""Merge mined candidates into one deduped pool."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config

CH = config.CHAINS_DIR
DATASET = config.DATA_DIR / "foresight_dataset.jsonl"
OUT = CH / "candidate_pool.jsonl"

KEEP = ("repo", "earlier_pr", "later_pr", "category", "evidence",
        "pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha",
        "pr1_date", "pr2_date", "flags")


def lang_of(repo: str) -> str:
    try:
        return config.get_target(repo).language
    except SystemExit:
        return "unknown"


REFERENCE = dt.datetime.fromisoformat(config.RECENCY_REFERENCE).replace(
    tzinfo=dt.timezone.utc)


def _parse_date(s: str | None) -> dt.datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def recency_tier(pr1_date: str | None, pr2_date: str | None) -> int | None:
    """Years back to the older PR."""
    a, b = _parse_date(pr1_date), _parse_date(pr2_date)
    if not (a and b):
        return None
    oldest = min(a, b)
    years = (REFERENCE - oldest).days / 365.25
    return max(1, math.ceil(years))


def main() -> None:
    validated = set()
    if DATASET.exists():
        validated = {json.loads(l)["instance_id"]
                     for l in DATASET.read_text().splitlines() if l.strip()}
    print(f"validated (excluded) chains: {len(validated)}")

    pool: dict[str, dict] = {}
    dup = excluded = 0
    for p in sorted(CH.glob("*_candidates.jsonl")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            repo, e, l = r.get("repo"), r.get("earlier_pr"), r.get("later_pr")
            # need four SHAs; earlier_pr may be 0
            shas = (r.get("pr1_base_sha"), r.get("pr1_head_sha"),
                    r.get("pr2_base_sha"), r.get("pr2_head_sha"))
            if not (repo and l and all(shas)):
                continue
            iid = f"{repo}__{e or 0}-{l}"
            if iid in validated:
                excluded += 1
                continue
            if iid in pool:
                dup += 1
                continue
            row = {"instance_id": iid, "language": lang_of(repo),
                   **{k: r.get(k) for k in KEEP}}
            # tag, never drop
            row["recency_tier"] = recency_tier(r.get("pr1_date"), r.get("pr2_date"))
            pool[iid] = row

    OUT.write_text("".join(json.dumps(v) + "\n" for v in pool.values()))

    # ---- report ----
    rows = list(pool.values())
    by_lang = Counter(r["language"] for r in rows)
    by_repo = Counter(r["repo"] for r in rows)
    print(f"\nde-dup: {dup} duplicate keys collapsed; {excluded} rows dropped as already-validated")
    print(f"POOL: {len(rows)} unique candidate chains -> {OUT.relative_to(config.REPO_ROOT)}")
    print(f"\nby language: {dict(by_lang)}")
    lang_repo = defaultdict(list)
    for repo, n in by_repo.most_common():
        lang_repo[lang_of(repo)].append((repo, n))
    for lang in sorted(lang_repo):
        tot = sum(n for _, n in lang_repo[lang])
        top = "  ".join(f"{r}:{n}" for r, n in lang_repo[lang][:12])
        print(f"\n--- {lang} ({tot}) ---\n  {top}")

    # ---- recency tiers (tagged, NOT filtered) ----
    tiers = Counter(r["recency_tier"] for r in rows)
    untiered = tiers.pop(None, 0)
    n_tiered = len(rows) - untiered
    print(f"\n--- recency_tier (reference {config.RECENCY_REFERENCE}, pinned) ---")
    print(f"{'tier':>6s} {'chains':>7s} {'cum':>7s} {'cum%':>6s}")
    cum = 0
    for t in sorted(tiers):
        cum += tiers[t]
        print(f"{t:>5d}y {tiers[t]:7d} {cum:7d} {cum / n_tiered * 100:5.1f}%")
    if untiered:
        print(f"  none {untiered:7d}   (missing/unparseable dates; kept, untiered)")
    n_docker = sum(n for t, n in tiers.items() if t <= config.DOCKER_RECENCY_YEARS)
    print(f"\nDocker-built subset `recency_tier <= {config.DOCKER_RECENCY_YEARS}`: "
          f"{n_docker}/{len(rows)} chains ({n_docker / len(rows) * 100:.1f}%). "
          f"NOTHING was dropped -- widen by raising the query, not by re-mining.")
    by_lang_docker = Counter(
        r["language"] for r in rows
        if r["recency_tier"] is not None
        and r["recency_tier"] <= config.DOCKER_RECENCY_YEARS)
    print(f"  by language: {dict(by_lang_docker)}")


if __name__ == "__main__":
    main()
