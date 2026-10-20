#!/usr/bin/env python3
"""Compute PR1 F2P and P2P tests in the image."""
from __future__ import annotations

import argparse
import json
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTANCES = ROOT / "docker" / "instances.json"
DATASET = ROOT / "data" / "foresight_dataset.jsonl"
OUT = ROOT / "data" / "pr1_tests.json"
REGISTRY = "docker.io/shaswatpatel123"


def eval_ready_ids() -> set:
    """eval_ready instance_ids from the CURRENT dataset (instances.json's flag is stale)."""
    if not DATASET.exists():
        return set()
    return {json.loads(l)["instance_id"] for l in DATASET.read_text().splitlines()
            if l.strip() and json.loads(l).get("eval_ready")}


def parse_junit(xml: str) -> dict:
    out = {}
    try:
        tree = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for case in tree.iter("testcase"):
        status = "passed"
        for ch in case:
            t = ch.tag.lower()
            if t == "failure":
                status = "failed"
            elif t == "error":
                status = "error"
            elif t == "skipped":
                status = "skipped"
        out[f"{case.get('classname','')}::{case.get('name','')}"] = status
    return out


def image_for(inst: dict) -> str:
    img = inst["image"]
    return img if "/" in img else f"{REGISTRY}/{img}"


def run_state(image: str, script: str, sha: str, overlay: str,
              test_files: list[str], platform: str = "") -> dict:
    """One in-image eval-primitive invocation -> {test_id: status}."""
    pf = ["--platform", platform] if platform else []
    cmd = ["docker", "run", "--rm", *pf, image, "bash", "-c",
           "%s %s %s /tmp/j.xml %s >/tmp/log 2>&1; cat /tmp/j.xml"
           % (script, sha, overlay, " ".join(test_files))]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    return parse_junit(p.stdout)


def compute(inst: dict, platform: str = "") -> dict:
    iid = inst["instance_id"]
    pr1_files = inst.get("pr1_test_files") or []
    res = {"instance_id": iid, "f2p": [], "p2p": [], "note": ""}
    if not pr1_files:
        res["note"] = "no pr1_test_files"
        return res
    img = image_for(inst)
    script = "run_state_django.sh" if inst.get("runner") == "django" else "run_state.sh"
    anticip = set(inst.get("anticipation_tests") or [])
    try:
        head = run_state(img, script, inst["pr1_head_sha"], "-", pr1_files, platform)
        base = run_state(img, script, inst["pr1_base_sha"], inst["pr1_head_sha"], pr1_files, platform)
    except subprocess.TimeoutExpired:
        res["note"] = "timeout"
        return res
    f2p = sorted(t for t, s in head.items()
                 if s == "passed" and base.get(t) in ("failed", "error") and t not in anticip)
    p2p = sorted(t for t, s in head.items()
                 if s == "passed" and base.get(t) == "passed"
                 and t not in anticip and t not in set(f2p))
    res.update(f2p=f2p, p2p=p2p, note=f"base={len(base)} head={len(head)}")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="",
                    help="force docker --platform (e.g. linux/amd64) on an arm64 host; "
                         "leave empty on a native amd64 HPC node")
    ap.add_argument("--repo", default="", help="restrict to one repo short name")
    ap.add_argument("--only-missing", action="store_true",
                    help="skip instances already in data/pr1_tests.json")
    ap.add_argument("--fully-gradeable-only", action="store_true")
    ap.add_argument("--eval-ready-only", action="store_true",
                    help="restrict to eval_ready instances per the CURRENT dataset")
    ap.add_argument("--ids", default="", help="comma-separated instance_ids (e.g. smoke test)")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    insts = json.load(open(INSTANCES))
    if args.repo:
        insts = [i for i in insts if i["repo"] == args.repo]
    if args.fully_gradeable_only:
        insts = [i for i in insts if i.get("fully_gradeable")]
    if args.eval_ready_only:
        er = eval_ready_ids()
        insts = [i for i in insts if i["instance_id"] in er]
    if args.ids:
        want = {x.strip() for x in args.ids.split(",") if x.strip()}
        insts = [i for i in insts if i["instance_id"] in want]

    result = json.loads(OUT.read_text()) if OUT.exists() else {}
    todo = [i for i in insts if not (args.only_missing and i["instance_id"] in result)]
    print(f"p2p: {len(insts)} instances, {len(todo)} to (re)compute, platform={args.platform or 'native'}",
          flush=True)

    # pull each image once first
    if args.platform:
        for img in sorted({image_for(i) for i in todo}):
            print(f"  pulling {img} ({args.platform})", flush=True)
            subprocess.run(["docker", "pull", "--platform", args.platform, img], check=False)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(compute, i, args.platform): i for i in todo}
        for k, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            result[r["instance_id"]] = {"f2p": r["f2p"], "p2p": r["p2p"]}
            OUT.write_text(json.dumps(result, indent=2) + "\n")  # incremental/resumable
            print(f"  [{k}/{len(todo)}] {r['instance_id']:34} "
                  f"F2P={len(r['f2p']):3} P2P={len(r['p2p']):3}  ({r['note']})", flush=True)

    tf = sum(len(v["f2p"]) for v in result.values())
    tp = sum(len(v["p2p"]) for v in result.values())
    print(f"\nwrote {OUT}: {len(result)} instances | {tf} F2P, {tp} P2P", flush=True)


if __name__ == "__main__":
    main()
