#!/usr/bin/env python3
"""Build one image tier (base, env, inst) in parallel."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config

ROOT = config.REPO_ROOT
DOCKER = ROOT / "docker"
RUNS = config.DATA_DIR / "docker_runs"
REGISTRY = "docker.io/shaswatpatel123/foresight"


def targets(tier: str, only: set[str], era: str) -> list[tuple[str, Path]]:
    """(tag, dockerfile) per image in the tier."""
    out: list[tuple[str, Path]] = []
    if tier == "base":
        for f in sorted((DOCKER / "base").glob("py*.Dockerfile")):
            out.append((f"base-{f.stem}", f))
    elif tier == "env":
        for d in sorted((DOCKER / "env").iterdir()):
            if (d / "Dockerfile").is_file():
                out.append((f"env-{d.name}", d / "Dockerfile"))
    elif tier == "inst":
        for d in sorted((DOCKER / "inst").iterdir()):
            if not (d / "Dockerfile").is_file():
                continue
            if era and not d.name.endswith(f"-{era}"):
                continue
            out.append((f"inst-{d.name}", d / "Dockerfile"))
    if only:
        out = [(t, f) for t, f in out if t in only or t.split("-", 1)[1] in only]
    return out


def free_gb(path: Path = Path("/")) -> float:
    import shutil
    return shutil.disk_usage(path).free / 1e9


def reclaim(min_free: float, keep_cache_gb: int = 10) -> float:
    """Prune the build cache when disk is low."""
    before = free_gb()
    if before >= min_free:
        return before
    subprocess.run(["docker", "builder", "prune", "-f",
                    f"--keep-storage={keep_cache_gb}GB"], capture_output=True)
    subprocess.run(["docker", "image", "prune", "-f"], capture_output=True)
    after = free_gb()
    print(f"  [disk] {before:.0f}GB free -> pruned cache -> {after:.0f}GB free",
          flush=True)
    return after


def build_one(tag_suffix: str, dockerfile: Path, platform: str, push: bool,
              prune: bool, logdir: Path, timeout: int) -> dict:
    tag = f"{REGISTRY}:{tag_suffix}"
    log = logdir / f"{tag_suffix}.log"
    # context is always docker/
    cmd = ["docker", "buildx", "build", "--platform", platform,
           "-t", tag, "-f", str(dockerfile.relative_to(DOCKER)),
           "--push" if push else "--load", "."]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=DOCKER, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        rc, out = 124, "TIMEOUT"
    dt = time.time() - t0
    log.write_text(f"$ {' '.join(cmd)}\n(cwd {DOCKER})\n\nrc={rc} ({dt:.0f}s)\n\n{out[-100000:]}")
    if rc == 0 and prune and push:
        # safe once pushed
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)
    return {"tag": tag_suffix, "rc": rc, "seconds": round(dt),
            "log": str(log.relative_to(ROOT)),
            "error": "" if rc == 0 else out[-1500:]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=["base", "env", "inst"])
    ap.add_argument("--only", default="", help="comma list of tags or names")
    ap.add_argument("--era", default="", choices=["", "pr1", "pr2"],
                    help="inst tier only: build just one era")
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="delete each image locally after a successful push")
    ap.add_argument("--min-free-gb", type=float, default=40.0,
                    help="prune the buildx cache whenever free disk drops below this")
    ap.add_argument("--timeout", type=int, default=5400)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    todo = targets(args.tier, only, args.era)
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"nothing to build for tier={args.tier}", file=sys.stderr)
        sys.exit(2)

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    logdir = RUNS / run_id / f"build-{args.tier}"
    logdir.mkdir(parents=True, exist_ok=True)
    print(f"building {len(todo)} image(s), tier={args.tier}, platform={args.platform}, "
          f"workers={args.workers}, push={args.push}")
    print(f"logs -> {logdir}")

    print(f"disk: {free_gb():.0f}GB free "
          f"(pruning the build cache below {args.min_free_gb}GB)")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(build_one, t, f, args.platform, args.push, args.prune,
                          logdir, args.timeout): t for t, f in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            flag = "ok " if r["rc"] == 0 else "FAIL"
            print(f"  [{i}/{len(todo)}] {flag} {r['tag']:44s} {r['seconds']:5d}s",
                  flush=True)
            if r["rc"] != 0:
                print(f"        {r['error'].splitlines()[-1][:160] if r['error'] else ''}",
                      flush=True)
            # check disk every 10 builds
            if i % 10 == 0:
                reclaim(args.min_free_gb)

    outp = RUNS / run_id / f"build_{args.tier}.jsonl"
    outp.write_text("".join(json.dumps(r) + "\n" for r in results))
    ok = sum(1 for r in results if r["rc"] == 0)
    print(f"\nbuilt {ok}/{len(results)}  -> {outp}")
    if ok != len(results):
        bad = [r["tag"] for r in results if r["rc"] != 0]
        print(f"FAILED ({len(bad)}): {bad[:12]}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
