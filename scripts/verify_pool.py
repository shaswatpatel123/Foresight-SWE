#!/usr/bin/env python3
"""Re-mine at pinned SHAs and diff the pool."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config, secrets
import mine_multiling as drv

MANIFEST = config.CHAINS_DIR / "multiling_manifest.json"
POOL = config.CHAINS_DIR / "candidate_pool.jsonl"


def _ids_from_rows(rows) -> set[str]:
    """Same keying + SHA-gate as build_candidate_pool.py."""
    out = set()
    for r in rows:
        repo, e, l = r.get("repo"), r.get("earlier_pr"), r.get("later_pr")
        shas = (r.get("pr1_base_sha"), r.get("pr1_head_sha"),
                r.get("pr2_base_sha"), r.get("pr2_head_sha"))
        if repo and l and all(shas):
            out.add(f"{repo}__{e or 0}-{l}")
    return out


def committed_ids_by_repo() -> dict[str, set[str]]:
    by: dict[str, set[str]] = {}
    for line in POOL.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            by.setdefault(r["repo"], set()).add(r["instance_id"])
    return by


def remine_ids(name: str, pin: dict, use_api: bool) -> set[str]:
    t = config.get_target(name)
    drv.ensure_repo(t)
    ref = pin.get("sha") or t.main_branch
    with tempfile.NamedTemporaryFile("r", suffix=".jsonl", delete=False) as tf:
        out = tf.name
    cmd = ["uv", "run", "foresight", "mine", "--repo", name, "--branch", ref, "--out", out]
    if use_api:
        cmd += ["--use-api"]
        if pin.get("since"):
            cmd += ["--since", pin["since"]]
    if t.changelog_dir or t.changelog_file or t.changelog_glob:
        cmd += ["--use-changelog"]
    subprocess.run(cmd, check=True, cwd=str(config.REPO_ROOT),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = [json.loads(l) for l in Path(out).read_text().splitlines() if l.strip()]
    Path(out).unlink(missing_ok=True)
    return _ids_from_rows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default="")
    ap.add_argument("--lang", default="all")
    args = ap.parse_args()

    if not MANIFEST.exists() or not POOL.exists():
        sys.exit("need multiling_manifest.json + candidate_pool.jsonl")
    manifest = json.loads(MANIFEST.read_text())["repos"]
    committed = committed_ids_by_repo()
    validated = set()
    ds = config.DATA_DIR / "foresight_dataset.jsonl"
    if ds.exists():
        validated = {json.loads(l)["instance_id"] for l in ds.read_text().splitlines() if l.strip()}
    use_api = bool(secrets.github_token())

    names = [r.strip() for r in args.repos.split(",") if r.strip()]
    if not names:
        pinned = set(manifest)
        names = [n for n in drv.select_targets(args.lang) if n in pinned]

    print(f"verifying {len(names)} repos against committed pool (use_api={use_api})\n")
    hdr = f"{'repo':14s} {'pinned':6s} {'remined':7s} {'match':6s}  verdict"
    print(hdr); print("-" * len(hdr))
    npass = 0
    for name in names:
        pin = manifest.get(name, {})
        want = committed.get(name, set())
        try:
            got = {i for i in remine_ids(name, pin, use_api) if i not in validated}
        except Exception as e:
            print(f"{name:14s} ERROR {type(e).__name__}: {e}"); continue
        inter = want & got
        ok = want == got
        npass += ok
        extra, missing = len(got - want), len(want - got)
        verdict = "PASS" if ok else f"DIFF (+{extra}/-{missing})"
        print(f"{name:14s} {len(want):6d} {len(got):7d} {len(inter):6d}  {verdict}")
    print("-" * len(hdr))
    print(f"\n{npass}/{len(names)} repos reproduce the committed pool exactly")


if __name__ == "__main__":
    main()
