#!/usr/bin/env python3
"""Clone and mine every target; pin SHAs in a manifest."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config, secrets

MANIFEST = config.CHAINS_DIR / "multiling_manifest.json"
YEARS = 2  # --use-api window


def select_targets(lang: str = "") -> list[str]:
    """Targets: non-Python, "all", or by language."""
    if lang == "all":
        return list(config.TARGETS)
    if lang:
        langs = {x.strip() for x in lang.split(",")}
        return [n for n, t in config.TARGETS.items() if t.language in langs]
    return [n for n, t in config.TARGETS.items() if t.language != "python"]


def _git(path: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, errors="replace", check=check)


def ensure_repo(t: config.RepoTarget) -> None:
    """Clone if missing; check out the branch."""
    if not (t.path / ".git").exists():
        print(f"  [clone] {t.slug} -> repos/{t.name}", flush=True)
        subprocess.run(["git", "clone", "--filter=blob:none",
                        f"https://github.com/{t.slug}.git", str(t.path)], check=True)
    # fetch non-default branches
    if _git(t.path, "rev-parse", "--verify", t.main_branch).returncode != 0:
        print(f"  [branch] checkout {t.main_branch} from origin", flush=True)
        _git(t.path, "fetch", "origin", t.main_branch)
        _git(t.path, "checkout", "-B", t.main_branch, f"origin/{t.main_branch}", check=True)


def head_sha(t: config.RepoTarget, ref: str) -> str:
    return _git(t.path, "rev-parse", ref).stdout.strip()


def candidate_count(name: str) -> int:
    p = config.CHAINS_DIR / f"{name}_candidates.jsonl"
    return sum(1 for _ in p.open()) if p.exists() else 0


def summary_counts(name: str) -> dict:
    p = config.CHAINS_DIR / f"{name}_summary.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    return {k: d.get(k, 0) for k in ("git_chains", "api_chains", "changelog_chains")}


def run_mine(t: config.RepoTarget, ref: str, since: str | None, use_api: bool,
             workers: int = 1) -> None:
    cmd = ["uv", "run", "foresight", "mine", "--repo", t.name, "--branch", ref]
    if use_api:
        cmd += ["--use-api"]
        if since:
            cmd += ["--since", since]
    if t.changelog_dir or t.changelog_file or t.changelog_glob or t.bugfix_commit_re:
        cmd += ["--use-changelog"]
        if workers > 1:
            cmd += ["--workers", str(workers)]
    print(f"  [mine] {' '.join(cmd[3:])}", flush=True)
    subprocess.run(cmd, check=True, cwd=str(config.REPO_ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=YEARS,
                    help="--use-api lookback window. The default of 2 was what the "
                         "original multilingual mine used, and it is the tightest "
                         "constraint on yield: the pool tiers chains to 5 years, so a "
                         "2-year API window cannot even see most of what is buildable.")
    ap.add_argument("--repos", default="", help="comma list; default = select_targets(--lang)")
    ap.add_argument("--lang", default="", help="'' = non-Python set; 'all'; or comma langs (e.g. go,python)")
    ap.add_argument("--reproduce", action="store_true", help="mine at the manifest's pinned SHA+since")
    ap.add_argument("--manifest-only", action="store_true", help="snapshot SHAs+counts, do not mine")
    ap.add_argument("--no-api", action="store_true", help="skip --use-api (local-only)")
    ap.add_argument("--workers", type=int, default=1,
                    help="SZZ blame concurrency per repo (identical output; see "
                         "`foresight mine --workers`)")
    args = ap.parse_args()

    names = [r.strip() for r in args.repos.split(",") if r.strip()] or select_targets(args.lang)
    token = secrets.github_token()
    use_api = bool(token) and not args.no_api
    if not use_api and not args.manifest_only:
        print("WARNING: no token / --no-api -> mining LOCAL-ONLY (a floor; API lifts it)",
              file=sys.stderr)

    pinned = {}
    if args.reproduce:
        if not MANIFEST.exists():
            sys.exit("--reproduce needs an existing manifest")
        pinned = json.loads(MANIFEST.read_text())["repos"]

    default_since = (datetime.now(timezone.utc) - timedelta(days=365 * args.years)
                     ).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = {"years": args.years, "repos": {}}
    for name in names:
        t = config.get_target(name)
        print(f"\n=== {name} ({t.language}) ===", flush=True)
        if not args.manifest_only:
            ensure_repo(t)
        if args.reproduce:
            ref = pinned.get(name, {}).get("sha") or t.main_branch
            since = pinned.get(name, {}).get("since")
        else:
            ref = t.main_branch
            since = default_since
        if not args.manifest_only:
            run_mine(t, ref, since, use_api, workers=args.workers)
        sha = head_sha(t, ref) if (t.path / ".git").exists() else ""
        entry = {
            "slug": t.slug, "branch": t.main_branch, "sha": sha,
            "since": since if use_api else None,
            "use_changelog": bool(t.changelog_dir or t.changelog_file or t.changelog_glob),
            "candidates": candidate_count(name), **summary_counts(name),
        }
        manifest["repos"][name] = entry
        print(f"  -> sha={sha[:10]} candidates={entry['candidates']}", flush=True)

    # subset run: merge manifest
    if MANIFEST.exists() and (args.repos or args.manifest_only):
        old = json.loads(MANIFEST.read_text())
        old.setdefault("repos", {}).update(manifest["repos"])
        old["years"] = args.years
        manifest = old
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")

    total = sum(e["candidates"] for e in manifest["repos"].values())
    print(f"\n===== manifest: {len(manifest['repos'])} repos, "
          f"{total} candidate chains -> {MANIFEST.relative_to(config.REPO_ROOT)} =====")
    for n, e in sorted(manifest["repos"].items(), key=lambda kv: -kv[1]["candidates"]):
        print(f"  {n:12s} {e['candidates']:5d}  sha={e['sha'][:10]}")


if __name__ == "__main__":
    main()
