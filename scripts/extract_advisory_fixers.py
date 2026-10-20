"""List fix commits named by security advisories."""
import argparse, collections, json, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEC = ROOT / "data" / "security"
ECO_LANG = {"npm": "javascript", "pip": "python", "go": "go"}

# selected repos: slug -> (short name, language)
SELECTED = {
    "aio-libs/aiohttp": ("aiohttp", "python"),
    "n8n-io/n8n": ("n8n", "javascript"),
    "withastro/astro": ("astro", "javascript"),
    "honojs/hono": ("hono", "javascript"),
    "helm/helm": ("helm", "go"),
}
COMMIT = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/commit/([0-9a-f]{7,40})", re.I)
PR = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "chains" / "security" / "fixers.jsonl"))
    ap.add_argument("--since", default="2021-09-07")
    args = ap.parse_args()

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows, seen = [], set()
    for eco in ECO_LANG:
        path = SEC / f"ghsa_{eco}.jsonl"
        if not path.exists():
            continue
        for line in path.open():
            adv = json.loads(line)
            if adv.get("withdrawn_at") or (adv.get("published_at") or "")[:10] < args.since:
                continue
            blob = " ".join(adv.get("references") or [])
            hits = collections.defaultdict(lambda: {"commits": [], "prs": []})
            for o, r, sha in COMMIT.findall(blob):
                hits[f"{o.lower()}/{r.lower()}"]["commits"].append(sha)
            for o, r, num in PR.findall(blob):
                hits[f"{o.lower()}/{r.lower()}"]["prs"].append(int(num))
            for slug, h in hits.items():
                if slug not in SELECTED:
                    continue
                key = (slug, adv["ghsa_id"])
                if key in seen:
                    continue
                seen.add(key)
                name, lang = SELECTED[slug]
                rows.append({
                    "repo": name, "slug": slug, "language": lang,
                    "ghsa_id": adv["ghsa_id"], "cve_id": adv.get("cve_id"),
                    "published_at": (adv.get("published_at") or "")[:10],
                    "severity": adv.get("severity"),
                    "cwes": [c.get("cwe_id") for c in (adv.get("cwes") or [])],
                    "cvss": (adv.get("cvss") or {}).get("score"),
                    "cvss_vector": (adv.get("cvss") or {}).get("vector_string"),
                    "summary": (adv.get("summary") or "")[:300],
                    "fix_commits": sorted(set(h["commits"])),
                    "fix_prs": sorted(set(h["prs"])),
                    "advisory_url": adv.get("html_url"),
                })

    rows.sort(key=lambda r: (r["language"], r["repo"], r["published_at"]))
    with out_path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    per = collections.Counter(r["repo"] for r in rows)
    commits = collections.Counter()
    for r in rows:
        commits[r["repo"]] += len(r["fix_commits"])
    print(f"{'repo':14} {'lang':11} {'advisories':>10} {'fix commits':>12} {'w/ PR':>6}")
    for slug, (name, lang) in SELECTED.items():
        sub = [r for r in rows if r["repo"] == name]
        withpr = sum(1 for r in sub if r["fix_prs"])
        print(f"{name:14} {lang:11} {per[name]:10} {commits[name]:12} {withpr:6}")
    print(f"\nTOTAL {len(rows)} advisories, {sum(commits.values())} fix commits -> {out_path}")


if __name__ == "__main__":
    main()
