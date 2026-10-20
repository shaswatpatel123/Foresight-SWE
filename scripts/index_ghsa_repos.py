"""Rank repos by advisories with a linked fix."""
import argparse, collections, json, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEC = ROOT / "data" / "security"
ECO_LANG = {"npm": "javascript", "pip": "python", "go": "go"}

COMMIT_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/commit/([0-9a-f]{7,40})", re.I)
PR_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)
SRC_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2021-09-07", help="recency window for *_recent")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--out", default=str(SEC / "ghsa_by_repo.jsonl"))
    args = ap.parse_args()

    per_repo = collections.defaultdict(lambda: collections.defaultdict(int))
    sev = collections.defaultdict(collections.Counter)
    cwes = collections.defaultdict(collections.Counter)
    ids = collections.defaultdict(set)

    for eco, lang in ECO_LANG.items():
        path = SEC / f"ghsa_{eco}.jsonl"
        if not path.exists():
            continue
        for line in path.open():
            adv = json.loads(line)
            if adv.get("withdrawn_at"):
                continue
            refs = adv.get("references") or []
            blob = " ".join(refs + [adv.get("source_code_location") or ""])
            commits = {(o.lower(), r.lower()) for o, r, _ in COMMIT_RE.findall(blob)}
            prs = {(o.lower(), r.lower()) for o, r, _ in PR_RE.findall(blob)}
            src = SRC_RE.search(adv.get("source_code_location") or "")
            home = {(src.group(1).lower(), src.group(2).lower())} if src else set()
            for repo in commits | prs | home:
                slug = "/".join(repo)
                key = (slug, eco)
                if adv["ghsa_id"] in ids[key]:
                    continue
                ids[key].add(adv["ghsa_id"])
                d = per_repo[key]
                d["advisories"] += 1
                recent = (adv.get("published_at") or "")[:10] >= args.since
                d["recent"] += recent
                if repo in commits:
                    d["commit_linked"] += 1
                    d["commit_linked_recent"] += recent
                if repo in prs:
                    d["pr_linked"] += 1
                    d["pr_linked_recent"] += recent
                if repo in commits or repo in prs:
                    d["fix_linked"] += 1
                    d["fix_linked_recent"] += recent
                sev[key][adv.get("severity") or "?"] += 1
                for c in adv.get("cwes") or []:
                    cwes[key][c.get("cwe_id")] += 1

    rows = []
    for (slug, eco), d in per_repo.items():
        rows.append({
            "slug": slug, "ecosystem": eco, "language": ECO_LANG[eco],
            **{k: d[k] for k in ("advisories", "recent", "fix_linked", "fix_linked_recent",
                                 "commit_linked", "commit_linked_recent",
                                 "pr_linked", "pr_linked_recent")},
            "severity": dict(sev[(slug, eco)]),
            "top_cwes": [c for c, _ in cwes[(slug, eco)].most_common(4)],
        })
    rows.sort(key=lambda r: (-r["fix_linked_recent"], -r["fix_linked"]))
    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    for lang in ("javascript", "python", "go"):
        sub = [r for r in rows if r["language"] == lang][: args.top]
        tot = sum(r["fix_linked_recent"] for r in rows if r["language"] == lang)
        print(f"\n=== {lang.upper()}  (fix-linked advisories since {args.since}: {tot}) ===")
        print(f"{'repo':45} {'fix5y':>6} {'fix_all':>8} {'adv_all':>8}  cwes")
        for r in sub:
            print(f"{r['slug']:45} {r['fix_linked_recent']:6} {r['fix_linked']:8} "
                  f"{r['advisories']:8}  {','.join(r['top_cwes'][:3])}")
    print(f"\nwrote {len(rows)} repo rows -> {args.out}")


if __name__ == "__main__":
    main()
