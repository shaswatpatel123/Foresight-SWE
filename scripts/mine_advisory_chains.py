"""Mine security chains into their own file."""
import argparse, collections, json, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from foresight.mining import advisory_szz as A  # noqa: E402

SECDIR = ROOT / "data" / "chains" / "security"
FIXERS = SECDIR / "fixers.jsonl"
OUT = SECDIR / "security_chains.jsonl"
REPORT = SECDIR / "mine_report.json"
MASTER = ROOT / "data" / "chains_master.jsonl"
ANCHOR = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")


def pr_index(repo: Path) -> dict[int, str]:
    """PR number -> commit, from the local clone."""
    out = {}
    log = A._git(repo, "log", "--format=%H%x1f%s", "--no-merges" if False else "HEAD")
    for line in log.splitlines():
        if "\x1f" not in line:
            continue
        sha, subj = line.split("\x1f", 1)
        m = ANCHOR.search(subj)
        if m:
            out.setdefault(int(m.group(1) or m.group(2)), sha)
    return out


def load_master_index() -> dict:
    """instance_id -> status (read-only)."""
    idx = {}
    if not MASTER.exists():
        return idx
    for line in MASTER.open(errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        i = r.get("instance_id")
        if not i:
            continue
        v = r.get("validation") or {}
        idx[i] = {"status": v.get("status") if isinstance(v, dict) else None,
                  "published": bool(r.get("published")),
                  "is_security_in_master": bool(r.get("is_security_vulnerability"))}
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT),
                    help="output jsonl; give each parallel repo its own file, "
                         "since the writer rewrites the whole file at the end")
    args = ap.parse_args()

    want = {r.strip() for r in args.repos.split(",") if r.strip()}
    advisories = collections.defaultdict(list)
    for line in FIXERS.open():
        r = json.loads(line)
        if not want or r["repo"] in want:
            advisories[r["repo"]].append(r)

    master = load_master_index()
    print(f"master index: {len(master)} instance_ids (read-only)", flush=True)

    SECDIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    existing = {}
    if out_path.exists():
        for line in out_path.open():
            r = json.loads(line)
            existing[r["instance_id"]] = r

    report = {}
    for name, advs in sorted(advisories.items()):
        repo = ROOT / "repos" / name
        if not repo.exists():
            print(f"SKIP {name}: not cloned", flush=True)
            continue
        t0 = time.time()
        lang = advs[0]["language"]
        prs = pr_index(repo)
        on_default = A.default_branch_commits(repo)
        reasons = collections.Counter()
        made = 0
        for adv in (advs[: args.limit] if args.limit else advs):
            shas = list(adv["fix_commits"])
            for n in adv.get("fix_prs") or []:
                if n in prs:
                    shas.append(prs[n])
            if not shas:
                reasons["no_commit_resolvable"] += 1
                continue
            picked = A.dedupe_cherry_picks(repo, shas, on_default)
            if not picked:
                reasons["commit_not_in_clone"] += 1
                continue
            reasons["cherry_picks_collapsed"] += len(shas) - len(picked)
            # default-branch commits first, and win
            picked.sort(key=lambda s: s not in on_default)
            for sha in picked:
                chain, why = A.mine_fixer(repo, name, lang, sha, adv)
                reasons[why] += 1
                if chain is None:
                    continue
                row = chain.__dict__.copy()
                seen = master.get(row["instance_id"])
                row["also_in_master"] = bool(seen)
                if seen:
                    row["master_status"] = seen
                prev = existing.get(row["instance_id"])
                row["pr2_on_default_branch"] = sha in on_default
                if prev:
                    for s in prev.get("sources", []):
                        if s not in row["sources"]:
                            row["sources"].append(s)
                    # keep the default-branch fix
                    if prev.get("pr2_on_default_branch") and not row["pr2_on_default_branch"]:
                        reasons["kept_default_branch_variant"] += 1
                        continue
                    reasons["superseded_by_default_branch"] += 1
                existing[row["instance_id"]] = row
                made += 1
        report[name] = {"advisories": len(advs), "chains": made,
                        "reasons": dict(reasons), "secs": round(time.time() - t0)}
        print(f"{name:12} advisories={len(advs):4} chains={made:4} "
              f"{dict(reasons)} [{report[name]['secs']}s]", flush=True)

    with out_path.open("w") as fh:
        for i in sorted(existing):
            fh.write(json.dumps(existing[i]) + "\n")
    overlap = sum(1 for r in existing.values() if r.get("also_in_master"))
    rp = REPORT if args.out == str(OUT) else out_path.with_suffix(".report.json")
    rp.write_text(json.dumps({"per_repo": report, "total_chains": len(existing),
                              "already_in_master": overlap}, indent=1))
    print(f"\nwrote {len(existing)} chains -> {out_path}  ({overlap} already in master)")


if __name__ == "__main__":
    main()
