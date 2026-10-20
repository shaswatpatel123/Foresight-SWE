"""Export master rows in the judge's input shape."""
import argparse, collections, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from foresight import store  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="")
    ap.add_argument("--repos", default="")
    ap.add_argument("--max-tier", type=int, default=0, help="0 = no cap")
    ap.add_argument("--only-unjudged", action="store_true", default=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stage1a-out", default="")
    args = ap.parse_args()

    langs = {l.strip() for l in args.lang.split(",") if l.strip()}
    repos = {r.strip() for r in args.repos.split(",") if r.strip()}

    pool, s1a = [], []
    skipped = collections.Counter()
    for r in store.load():
        if langs and r.get("language") not in langs:
            continue
        if repos and r.get("repo") not in repos:
            continue
        f = r.get("filters") or {}
        if not f.get("pr2_has_test_changes"):
            skipped["no_pr2_test_changes"] += 1
            continue
        tier = f.get("recency_tier")
        if args.max_tier and (tier is None or tier > args.max_tier):
            skipped["above_tier_cap"] += 1
            continue
        if args.only_unjudged and (r.get("judge") or {}).get("verdicts"):
            skipped["already_judged"] += 1
            continue
        if not (r.get("pr2_base_sha") and r.get("pr2_head_sha")):
            skipped["missing_shas"] += 1
            continue
        ev = "; ".join(s.get("evidence", "") for s in (r.get("sources") or []))[:400]
        pool.append({
            "instance_id": r["instance_id"], "repo": r["repo"],
            "language": r.get("language"), "category": r.get("category"),
            "earlier_pr": r.get("introducer_pr"), "later_pr": r.get("fixer_pr"),
            "pr1_base_sha": r.get("pr1_base_sha"), "pr1_head_sha": r.get("pr1_head_sha"),
            "pr2_base_sha": r["pr2_base_sha"], "pr2_head_sha": r["pr2_head_sha"],
            "pr1_date": r.get("pr1_date"), "pr2_date": r.get("pr2_date"),
            "recency_tier": tier, "evidence": ev,
            "is_security_vulnerability": bool(r.get("is_security_vulnerability")),
        })
        s1a.append({"instance_id": r["instance_id"], "pr2_has_test_changes": True})

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in pool))
    s1a_path = pathlib.Path(args.stage1a_out or str(out).replace(".jsonl", "_stage1a.jsonl"))
    s1a_path.write_text("".join(json.dumps(r) + "\n" for r in s1a))

    print(f"{len(pool)} judge-ready rows -> {out}")
    print(f"  by language: {dict(collections.Counter(r['language'] for r in pool))}")
    print(f"  by repo: {dict(collections.Counter(r['repo'] for r in pool).most_common())}")
    print(f"  security-flagged: {sum(1 for r in pool if r['is_security_vulnerability'])}")
    print(f"  skipped: {dict(skipped)}")


if __name__ == "__main__":
    main()
