"""Merge security chains into the master table."""
import argparse, collections, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from foresight import store  # noqa: E402

SEC = ROOT / "data" / "chains" / "security" / "security_chains.jsonl"
# bookkeeping that only meant something while the file was separate
DROP = ("also_in_master", "master_status", "screens", "gradeable_candidate")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--src", default=str(SEC))
    args = ap.parse_args()

    master = store.load()
    by_id = {r["instance_id"]: r for r in master}
    sec = [json.loads(l) for l in pathlib.Path(args.src).open() if l.strip()]

    stats = collections.Counter()
    for s in sec:
        s = {k: v for k, v in s.items() if k not in DROP}
        iid = s["instance_id"]
        cur = by_id.get(iid)
        if cur is None:
            s["is_security_vulnerability"] = True
            by_id[iid] = s
            stats["added"] += 1
            stats[f"added:{s['language']}"] += 1
            continue
        # existing chain: add sources only
        srcs = cur.setdefault("sources", [])
        for ev in s.get("sources", []):
            if ev not in srcs:
                srcs.append(ev)
        cur["security"] = s.get("security")
        cur["is_security_vulnerability"] = True
        if s.get("szz") and not cur.get("szz"):
            cur["szz"] = s["szz"]
        stats["merged_into_existing"] += 1

    rows = sorted(by_id.values(), key=lambda r: r["instance_id"])
    sec_total = sum(1 for r in rows if r.get("is_security_vulnerability"))
    print(f"master before: {len(master)}   security rows in: {len(sec)}")
    for k in sorted(stats):
        print(f"  {k:24} {stats[k]}")
    print(f"master after:  {len(rows)}   flagged is_security_vulnerability: {sec_total}")
    if args.dry_run:
        print("dry run: nothing written")
        return
    n = store.save(rows)          # snapshots the previous master first
    print(f"wrote {n} rows to {store.MASTER} (previous version snapshotted)")


if __name__ == "__main__":
    main()
