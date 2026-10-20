#!/usr/bin/env python3
"""Validate chains in their images: five states each."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config
from foresight.langspec import changed_test_files

ROOT = config.REPO_ROOT
MANIFEST = ROOT / "docker" / "validate_instances.json"
RUNS = config.DATA_DIR / "docker_runs"
REGISTRY = "docker.io/shaswatpatel123/foresight"

def resolve_sha(repo: str, ref: str) -> str:
    """Ref -> full SHA."""
    if not ref:
        return ""
    t = config.get_target(repo)
    out = subprocess.run(["git", "-C", str(t.path), "rev-parse", ref],
                         capture_output=True, text=True, errors="replace")
    s = out.stdout.strip()
    return s if len(s) == 40 else ref


def test_files(repo: str, base: str, head: str) -> list[str]:
    """Test files a PR changed."""
    t = config.get_target(repo)
    return changed_test_files(t.path, base, head, t.language)


def parse_junit(text: str) -> dict[tuple[str, str], str]:
    """(classname, name) -> pass or fail."""
    out: dict[tuple[str, str], str] = {}
    try:
        # strip: XML decl must be first
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        return out
    for tc in root.iter("testcase"):
        key = (tc.get("classname", ""), tc.get("name", ""))
        bad = any(tc.find(t) is not None for t in ("failure", "error"))
        skipped = tc.find("skipped") is not None
        if skipped:
            continue
        out[key] = "fail" if bad else "pass"
    return out


COMPILED_REPOS = {"aiohttp", "numpy", "scipy", "pandas", "matplotlib", "sklearn",
                  "astropy", "pillow"}


def run_era(image: str, runner: str, repo: str, recipe_sha: str,
            states: list[dict], logdir: Path, platform: str, timeout: int) -> dict:
    """Run one era's states in one container."""
    logdir.mkdir(parents=True, exist_ok=True)
    spec = {"recipe_sha": recipe_sha,
            "compiled": 1 if repo in COMPILED_REPOS else 0,
            "runner": runner,
            "settings": getattr(config.get_target(repo), "django_settings", "test_sqlite"),
            "states": states}
    spec_json = json.dumps(spec)
    inner = ("cat > /tmp/spec.json <<'FORESIGHT_SPEC_EOF'\n" + spec_json +
             "\nFORESIGHT_SPEC_EOF\nbash /usr/local/bin/run_era.sh /tmp/spec.json")

    tag = image if "/" in image else f"{REGISTRY}:env-{image}"
    cmd = ["docker", "run", "--rm"]
    if platform:
        cmd += ["--platform", platform]
    for s in ("run_era.sh", "run_state.sh", "run_state_django.sh", "build_env.sh",
              "django_to_junit.py"):
        cmd += ["-v", f"{ROOT / 'docker' / s}:/usr/local/bin/{s}:ro"]
    recipes = ROOT / "docker" / "install" / repo
    if repo and recipes.is_dir():
        cmd += ["-v", f"{recipes}:/opt/install:ro"]
    cmd += [tag, "bash", "-lc", inner]

    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
        out, rc = p.stdout, p.returncode
    except subprocess.TimeoutExpired:
        out, rc = "", 124
    era = states[0]["label"]
    log = logdir / f"era_{era}.log"
    log.write_text(f"$ {' '.join(cmd[:12])} ...\n\nrc={rc} ({time.time()-t0:.0f}s)\n\n{out[-400000:]}")

    # split output into states, by partition
    results: dict[str, dict] = {}
    rest = out
    for st in states:
        marker = f"---STATE {st['label']}---"
        _, found, rest = rest.partition(marker)
        if not found:
            results[st["label"]] = {"ok": False, "outcomes": {}, "rc": rc,
                                    "run_state_rc": "", "run_tail": out[-2000:],
                                    "log": str(log.relative_to(ROOT))}
            continue
        section, _, rest = rest.partition("---STATE ")
        rest = "---STATE " + rest if rest else rest
        def take(marker: str, s: str) -> tuple[str, str]:
            head, _, tail = s.partition(marker)
            return head, tail
        _, s1 = take("---RC---", section)
        rs_rc, s2 = take("---JUNIT---", s1)
        xml, run_tail = take("---RUN---", s2)
        outcomes = parse_junit(xml)
        results[st["label"]] = {"ok": bool(outcomes), "outcomes": outcomes, "rc": rc,
                                "run_state_rc": rs_rc.strip(),
                                "run_tail": run_tail[-2000:],
                                "log": str(log.relative_to(ROOT))}
    return results


def derive(o: dict) -> dict:
    """Test sets from the five states."""
    s1, s2, s3 = o["pr2_head"], o["pr2_base"], o["cross"]
    s4, s5 = o["pr1_head"], o["pr1_base"]
    # PR2 fixed these
    pr2_f2p = {k for k, v in s1.items() if v == "pass" and s2.get(k) == "fail"}
    cross = {
        "pr2_f2p": len(pr2_f2p),
        "failed": len([k for k in pr2_f2p if s3.get(k) == "fail"]),   # bug live at PR1
        "passed": len([k for k in pr2_f2p if s3.get(k) == "pass"]),   # bug absent at PR1
        "absent": len([k for k in pr2_f2p if k not in s3]),           # never ran at PR1
    }
    anticipation = sorted(f"{c}::{n}" for (c, n) in pr2_f2p
                          if s3.get((c, n)) == "fail")
    f2p = sorted(f"{c}::{n}" for (c, n), v in s4.items()
                 if v == "pass" and s5.get((c, n)) == "fail")
    ant_set = set(anticipation)
    p2p = sorted(f"{c}::{n}" for (c, n), v in s4.items()
                 if v == "pass" and s5.get((c, n)) == "pass"
                 and f"{c}::{n}" not in ant_set)
    p2p = [t for t in p2p if t not in set(f2p)]
    return {"anticipation_tests": anticipation, "pr1_f2p_tests": f2p,
            "pr1_p2p_tests": p2p, "cross_breakdown": cross}


def status_of(states: dict, sets: dict, has_pr1_tests: bool) -> str:
    """Validation status for one chain."""
    if not states["pr2_head"]["ok"] or not states["pr2_base"]["ok"]:
        return "env_failed_pr2"
    if not states["cross"]["ok"]:
        return "env_failed_cross"
    if not sets["anticipation_tests"]:
        # why no anticipation tests
        cb = sets["cross_breakdown"]
        if not cb["pr2_f2p"]:
            return "pr2_not_fail_to_pass"   # PR2 is not the fixer
        if cb["absent"] and not cb["passed"]:
            return "cross_not_collected"    # tests can't run at pr1_head
        if cb["passed"] and not cb["absent"]:
            return "no_pr1_failure"         # wrong introducer
        return "cross_partial"              # mixed
    if not has_pr1_tests:
        return "validated_ant_only"        # PR1 has no tests
    if not states["pr1_head"]["ok"] or not states["pr1_base"]["ok"]:
        # PR1 tests didn't collect
        return "validated_pr1_uncollectible"
    return "validated"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(MANIFEST),
                    help="which validate_instances manifest to run. Manifests are per "
                         "LANGUAGE (validate_instances.json / "
                         "validate_instances_javascript.json) because each describes "
                         "the images of one plan; without this flag a JS chain is "
                         "unreachable and the run silently validates Python instead.")
    ap.add_argument("--image", default="", help="only instances routed to this image")
    ap.add_argument("--ids", default="", help="comma-separated instance_ids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--platform", default="",
                    help="e.g. linux/amd64 when running x86 images on arm64")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--expect", default="",
                    help="known-good f2p/p2p json; mismatches FAIL the run")
    ap.add_argument("--force", action="store_true",
                    help="re-validate chains that already have a result.json in this "
                         "run dir (default: resume, skipping them)")
    ap.add_argument("--no-recipe-guard", action="store_true",
                    help="validate even chains with no install recipe (they will "
                         "almost certainly report env_failed -- only useful to "
                         "measure the no-recipe fallback itself)")
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()

    # manifest SHAs are resolved
    rows = json.loads(Path(args.manifest).read_text())
    if args.image:
        rows = [m for m in rows if args.image in m["images"].values()]
    if args.ids:
        want = {s.strip() for s in args.ids.split(",")}
        rows = [m for m in rows if m["instance_id"] in want]
    if args.limit:
        rows = rows[: args.limit]

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    rundir = RUNS / run_id / (args.image or "mixed") / "validate"
    rundir.mkdir(parents=True, exist_ok=True)
    print(f"validating {len(rows)} instance(s) -> {rundir}")

    def one(m: dict) -> dict:
        iid, p = m["instance_id"], m
        # resume: skip finished chains
        done = rundir / iid / "result.json"
        if done.is_file() and not args.force:
            try:
                return json.loads(done.read_text())
            except json.JSONDecodeError:
                pass          # corrupt: redo
        # image not built yet: skip
        missing = [s for s in ("pr2_head", "pr2_base", "cross")
                   if not m["images"].get(s)]
        if missing:
            return {"instance_id": iid, "repo": m["repo"],
                    "status": "image_not_built", "missing_states": missing,
                    "anticipation_tests": [], "pr1_f2p_tests": [], "pr1_p2p_tests": []}
        # need both era recipes
        if not args.no_recipe_guard:
            absent = [f"{era}:{sha[:12]}" for era, sha in
                      (("pr1", m["pr1_base_sha"]), ("pr2", m["pr2_head_sha"]))
                      if not (ROOT / "docker" / "install" / m["repo"]
                              / f"{sha[:12]}.json").is_file()]
            if absent:
                return {"instance_id": iid, "repo": m["repo"],
                        "status": "recipe_missing", "missing_recipes": absent,
                        "anticipation_tests": [], "pr1_f2p_tests": [],
                        "pr1_p2p_tests": []}
        tf2 = test_files(m["repo"], p["pr2_base_sha"], p["pr2_head_sha"])
        tf1 = test_files(m["repo"], p["pr1_base_sha"], p["pr1_head_sha"])
        if not tf2:
            return {"instance_id": iid, "status": "no_test_files"}
        ld = rundir / iid
        # recipe commit per era
        r1, r2 = p["pr1_base_sha"], p["pr2_head_sha"]
        plan = [
            ("pr2_head", m["images"]["pr2_head"], p["pr2_head_sha"], "-", tf2, r2),
            ("pr2_base", m["images"]["pr2_base"], p["pr2_base_sha"], p["pr2_head_sha"], tf2, r2),
            ("cross", m["images"]["cross"], p["pr1_head_sha"], p["pr2_head_sha"], tf2, r1),
        ]
        # skip PR1 states if unbuilt
        if tf1 and not (m["images"].get("pr1_head") and m["images"].get("pr1_base")):
            tf1 = []
        if tf1:
            plan += [
                ("pr1_head", m["images"]["pr1_head"], p["pr1_head_sha"], "-", tf1, r1),
                ("pr1_base", m["images"]["pr1_base"], p["pr1_base_sha"], p["pr1_head_sha"], tf1, r1),
            ]
        # one container per era; cross is PR1
        states, state_shas = {}, {}
        by_era: dict[tuple, list] = {}
        for label, image, sha, overlay, tests, rsha in plan:
            by_era.setdefault((rsha, image), []).append(
                {"label": label, "sha": sha, "overlay": overlay,
                 "junit": f"/tmp/foresight_{label}.xml", "tests": tests})
            # record code and test commits
            state_shas[label] = {
                "code": resolve_sha(m["repo"], sha),
                "tests": resolve_sha(m["repo"], sha if overlay == "-" else overlay)}
        for (rsha, image), group in by_era.items():
            states.update(run_era(image, m["runner"], m["repo"], rsha, group,
                                  ld, args.platform, args.timeout * len(group)))
        for label in ("pr1_head", "pr1_base"):
            states.setdefault(label, {"ok": False, "outcomes": {}, "rc": -1, "log": ""})
        sets = derive({k: v["outcomes"] for k, v in states.items()})
        rec = {"instance_id": iid, "repo": m["repo"], "split_image": m["split_image"],
               "images": m["images"], **sets,
               "status": status_of(states, sets, bool(tf1)),
               "state_logs": {k: v["log"] for k, v in states.items()},
               "n_outcomes": {k: len(v["outcomes"]) for k, v in states.items()},
               "state_shas": state_shas}
        (ld / "result.json").write_text(json.dumps(rec, indent=2) + "\n")
        return rec

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, m): m["instance_id"] for m in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            print(f"  [{i}/{len(rows)}] {r['instance_id']:28s} {r['status']:22s} "
                  f"ant={len(r.get('anticipation_tests', []))} "
                  f"f2p={len(r.get('pr1_f2p_tests', []))} "
                  f"p2p={len(r.get('pr1_p2p_tests', []))}", flush=True)

    outp = RUNS / run_id / f"validated_{args.image or 'mixed'}.jsonl"
    outp.write_text("".join(json.dumps(r) + "\n" for r in results))

    ok = [r for r in results if r["status"].startswith("validated")]
    fg = [r for r in ok if r.get("pr1_f2p_tests")]
    print(f"\nvalidated {len(ok)}/{len(results)}  (fully-gradeable {len(fg)})")
    from collections import Counter
    print("status:", dict(Counter(r["status"] for r in results)))

    if args.expect:
        exp = json.loads(Path(args.expect).read_text())
        bad, trivial = [], []
        for r in results:
            e = exp.get(r["instance_id"])
            if not e:
                continue
            # not validated counts as mismatch
            if not str(r.get("status", "")).startswith("validated"):
                bad.append(f"{r['instance_id']}({r.get('status')})")
                continue
            if (sorted(e.get("f2p", [])) != r.get("pr1_f2p_tests", []) or
                    sorted(e.get("p2p", [])) != r.get("pr1_p2p_tests", [])):
                bad.append(r["instance_id"])
            elif not e.get("f2p") and not e.get("p2p"):
                # empty ground truth: vacuous
                trivial.append(r["instance_id"])
        checked = sum(1 for r in results if r["instance_id"] in exp)
        good = checked - len(bad)
        print(f"\n--expect: {good}/{checked} match ground truth "
              f"({good - len(trivial)} substantive, {len(trivial)} vacuous "
              f"[GT empty on both f2p and p2p])")
        if bad:
            print(f"MISMATCH on {len(bad)}: {bad[:10]}", file=sys.stderr)
            sys.exit(1)
    print(f"\n-> {outp}")


if __name__ == "__main__":
    main()
