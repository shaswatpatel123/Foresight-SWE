"""Add newly mined chains to the master table."""
import argparse, collections, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from foresight import config, store  # noqa: E402
from build_master import MINING_FAMILY, iid_of, iter_rows  # noqa: E402

CH = ROOT / "data" / "chains"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default="")
    ap.add_argument("--lang", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    want = {r.strip() for r in args.repos.split(",") if r.strip()}
    if args.lang:
        langs = {l.strip() for l in args.lang.split(",") if l.strip()}
        want |= {n for n, t in config.TARGETS.items() if t.language in langs}
    if not want:
        sys.exit("give --repos or --lang")

    master = store.load()
    by_id = {r["instance_id"]: r for r in master}
    stats = collections.Counter()

    for p in sorted(CH.glob("*.jsonl")):
        fam = p.name.split("_", 1)[1].replace(".jsonl", "") if "_" in p.name else ""
        src = MINING_FAMILY.get(fam)
        repo = p.name.split("_", 1)[0]
        if not src or repo not in want or "validated" in p.name:
            continue
        for r in iter_rows(p):
            i = iid_of(r)
            if not i or r.get("earlier_pr") is None:
                continue
            row = by_id.get(i)
            if row is None:
                row = {"instance_id": i, "sources": [],
                       "repo": r["repo"],
                       "language": config.TARGETS[r["repo"]].language,
                       "introducer_pr": r["earlier_pr"], "fixer_pr": r["later_pr"]}
                by_id[i] = row
                stats[f"added:{row['language']}"] += 1
            else:
                stats["already_present"] += 1
            for k in ("pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha",
                      "pr1_date", "pr2_date", "category"):
                if r.get(k) and not row.get(k):
                    row[k] = r[k]
            ev = {"source": src, "evidence": r.get("evidence", ""),
                  "evidence_locus": "commit_msg" if src == "git_text" else None,
                  "found_in": p.name}
            if not any(e.get("source") == ev["source"]
                       and e.get("evidence") == ev["evidence"]
                       for e in row["sources"]):
                row["sources"].append(ev)
                stats["edges_added"] += 1

    rows = sorted(by_id.values(), key=lambda r: r["instance_id"])
    print(f"master before {len(master)} -> after {len(rows)}")
    for k in sorted(stats):
        print(f"  {k:24} {stats[k]}")
    if args.dry_run:
        print("dry run: nothing written")
        return
    print(f"wrote {store.save(rows)} rows (previous version snapshotted)")


if __name__ == "__main__":
    main()
