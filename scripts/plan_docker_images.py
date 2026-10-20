#!/usr/bin/env python3
"""Plan the fewest env images for buildable chains."""
from __future__ import annotations

import argparse
import collections
import pathlib
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import (config, goversion, introducer_guard,
                       nodeversion, pyversion, store)
from foresight.gates import gate_reason
from foresight.langspec import changed_test_files

ROOT = config.REPO_ROOT
OUT = config.CHAINS_DIR / "docker_plan.json"

ERAS = ("pr1", "pr2")


# --- per-language toolchain adapters

class _PyToolchain:
    key, tag = "python", "py"

    def era(self, repo_path, base, head):
        lo, hi, src, status = pyversion.era_interval(repo_path, base, head)
        return (lo, hi), src, status

    def probe(self, allowed):
        _lo, hi = allowed          # ends disagree: try hi
        return (hi, hi)

    def describe(self, allowed):
        lo, hi = allowed
        return {"lo": self.fmt(lo), "hi": self.fmt(hi)}

    def cover(self, alloweds):
        return pyversion.pierce(list(alloweds))

    def contains(self, allowed, pin):
        lo, hi = allowed
        return lo <= pin <= hi

    def fmt(self, pin):
        return f"{pin[0]}.{pin[1]}"

    def parse_version(self, s):
        return tuple(int(x) for x in str(s).split(".")[:2])


class _NodeToolchain:
    key, tag = "node", "node"

    def era(self, repo_path, base, head):
        return nodeversion.era_majors(repo_path, base, head)

    def probe(self, allowed):
        # already narrowed
        return allowed

    def describe(self, allowed):
        return {"majors": ",".join(str(m) for m in sorted(allowed))}

    def cover(self, alloweds):
        return nodeversion.cover(list(alloweds))

    def contains(self, allowed, pin):
        return pin in allowed

    def fmt(self, pin):
        return nodeversion.fmt(pin)

    def parse_version(self, s):
        return int(str(s).split(".")[0])


class _GoToolchain:
    """Go: an interval, like Python."""

    key, tag = "go", "go"

    def era(self, repo_path, base, head):
        lo, hi, src, status = goversion.era_interval(repo_path, base, head)
        return (lo, hi), src, status

    def probe(self, allowed):
        _lo, hi = allowed
        return (hi, hi)

    def describe(self, allowed):
        lo, hi = allowed
        return {"lo": self.fmt(lo), "hi": self.fmt(hi)}

    def cover(self, alloweds):
        return goversion.cover(list(alloweds))

    def contains(self, allowed, pin):
        lo, hi = allowed
        return lo <= pin <= hi

    def fmt(self, pin):
        return f"{pin[0]}.{pin[1]}"

    def parse_version(self, s):
        return tuple(int(x) for x in str(s).split(".")[:2])


TOOLCHAINS = {"python": _PyToolchain(),
              "go": _GoToolchain(),
              "javascript": _NodeToolchain(),
              "typescript": _NodeToolchain()}


def era_shas(row: dict, era: str) -> tuple[str, str]:
    """(base, head) commits of one era."""
    if era == "pr1":
        return row["pr1_base_sha"], row["pr1_head_sha"]
    return row["pr2_base_sha"], row["pr2_head_sha"]


def load_prior(path: str, tc) -> dict[tuple[str, str], tuple[int, int]]:
    """Earlier (instance, era) -> version pins."""
    p = Path(path) if path else None
    if not p or not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    def version_of(tag: str):
        """Version from an env tag."""
        try:
            if "-node" in tag:
                return int(tag.rsplit("-node", 1)[1].split(".")[0])
            if "py" in tag:
                a, b = tag.rsplit("py", 1)[1].split(".")[:2]
                return int(a), int(b)
        except ValueError:
            return None
        return None

    out: dict[tuple[str, str], tuple[int, int]] = {}
    if isinstance(data, dict) and "images" in data:
        for img in data["images"]:
            # `version`, or the toolchain key
            raw = img.get("version", img.get("python", img.get("node")))
            v = tc.parse_version(raw) if raw is not None else None
            if v is None:
                continue
            for i in img.get("pr1_instances", []):
                out[(i, "pr1")] = v
            for i in img.get("pr2_instances", []):
                out[(i, "pr2")] = v
    elif isinstance(data, list):
        for m in data:
            im = m.get("images") or {}
            for era, state in (("pr1", "pr1_base"), ("pr2", "pr2_head")):
                v = version_of(im.get(state, ""))
                if v:
                    out[(m["instance_id"], era)] = v
    return out



def _relocated_test_dirs(row: dict) -> list[str]:
    """PR2 test dirs missing at pr1_base (diagnostic only)."""
    try:
        t = config.get_target(row["repo"])
    except SystemExit:
        return []
    if not t.path.exists():
        return []
    files = changed_test_files(t.path, row["pr2_base_sha"], row["pr2_head_sha"],
                               t.language)
    base = row["pr1_base_sha"]
    missing, seen = [], set()
    for f in files:
        d = f.rsplit("/", 1)[0] if "/" in f else ""
        if not d or d in seen:
            continue
        seen.add(d)
        rc = subprocess.run(["git", "-C", str(t.path), "cat-file", "-e", f"{base}:{d}"],
                            capture_output=True).returncode
        if rc != 0:
            missing.append(d)
    return missing


def write_plan_to_master(images: list[dict], eras_split: list[str], tc, lang: str,
                         max_tier: int, run_id: str, path=None) -> str:
    """Write the plan to the master table."""
    assigned: dict[str, dict[str, tuple[str, str]]] = {}
    for img in images:
        env = f"{img['repo']}-{tc.tag}{img['version']}"
        interp = f"{tc.key}{img['version']}"
        for era in ERAS:
            for iid in img[f"{era}_instances"]:
                assigned.setdefault(iid, {})[era] = (env, interp)

    split = set(eras_split)
    sha_for = {"pr1": "pr1_base_sha", "pr2": "pr2_head_sha"}
    updates: dict[str, dict] = {}
    for row in store.load(path or store.MASTER):
        if row.get("language") != lang:
            continue
        if (row.get("plan") or {}).get("provenance") == "from_validation":
            continue
        iid = row["instance_id"]
        if iid in assigned:
            block = {"outcome": "ok", "provenance": "current_plan", "run_id": run_id,
                     "eras_split": iid in split}
            for era, (env, interp) in assigned[iid].items():
                block[era] = {"interpreter": interp, "env_image": env,
                              "build_sha": (row.get(sha_for[era]) or "")[:12] or None}
            updates[iid] = block
        else:
            updates[iid] = {"outcome": "not_applicable",
                            "reason": gate_reason(row, max_tier)}
    return str(store.update("plan", updates, path=path or store.MASTER))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tier", type=int, default=config.DOCKER_RECENCY_YEARS)
    ap.add_argument("--lang", default="python")
    ap.add_argument("--include-anticipation-only", action="store_true",
                    help="also build chains whose PR1 changed no test file. They can "
                         "never be fully gradeable (no pr1_f2p possible) but do carry "
                         "a real anticipation set, so they are usable for an "
                         "anticipation-only metric or the PR2-direct oracle.")
    ap.add_argument("--out", default="",
                    help="default: docker_plan.json for python, "
                         "docker_plan_<lang>.json otherwise -- so planning a new "
                         "language cannot clobber the live Python plan")
    ap.add_argument("--prev", default=str(ROOT / "docker" / "validate_instances.json"),
                    help="previous plan or manifest, for sticky assignment. A slot "
                         "already assigned an interpreter keeps it while its interval "
                         "allows, so recipes accepted under that interpreter stay "
                         "valid. Pass '' to re-plan from scratch.")
    ap.add_argument("--run-id", default="",
                    help="stamped onto every plan block this run writes; "
                         "default plan-<today>")
    ap.add_argument("--store", default="",
                    help="chain file to plan from; defaults to the master. Same "
                         "escape hatch prefilter_test_changes.py has -- the store "
                         "layer is path-parameterised, and a side file (the security "
                         "stratum) must be plannable without writing the master while "
                         "another process owns it.")
    ap.add_argument("--no-master", action="store_true",
                    help="emit the projection but leave chains_master.jsonl alone")
    args = ap.parse_args()
    run_id = args.run_id or f"plan-{dt.date.today().isoformat()}"

    if args.lang not in TOOLCHAINS:
        sys.exit(f"no toolchain for --lang {args.lang!r}; "
                 f"have {sorted(TOOLCHAINS)}")
    tc = TOOLCHAINS[args.lang]
    out_path = Path(args.out) if args.out else (
        OUT if args.lang == "python"
        else config.CHAINS_DIR / f"docker_plan_{args.lang}.json")

    # drop introducers after the report
    date_flags = introducer_guard.load_flags()

    seen, rows, ant_only, impossible, relocated = set(), [], [], [], []
    skipped: collections.Counter = collections.Counter()
    for p in store.load(pathlib.Path(args.store) if args.store else store.MASTER):
        iid = p["instance_id"]
        if p.get("language") != args.lang:
            continue
        judge = p.get("judge") or {}
        if not judge:
            skipped["never_judged"] += 1
            continue
        seen.add(iid)                       # judged chains
        if not judge.get("gradeable"):
            skipped["not_gradeable"] += 1
            continue
        if iid in date_flags:
            impossible.append({"instance_id": iid, **{
                k: date_flags[iid].get(k) for k in
                ("ticket_opened", "introducer_date", "audit_file")}})
            continue
        f = p.get("filters") or {}
        t = f.get("recency_tier")
        if t is None or t > args.max_tier:
            skipped["tier_above_max"] += 1
            continue
        # pre-docker chains stay out
        if (p.get("validation") or {}).get("run_id") == "v1-local":
            skipped["validated_pre_docker"] += 1
            continue
        # PR1 without tests: anticipation-only
        if f.get("pr1_has_test_changes") is False and not args.include_anticipation_only:
            ant_only.append(iid)
            continue
        rows.append(p)

    # ---- one allowed-version set per (chain, era) --------------------------------
    per_repo: dict[str, list] = collections.defaultdict(list)
    undeclared, probed = [], []
    src_tally = collections.Counter()

    for p in rows:
        repo_path = config.REPOS_DIR / p["repo"]
        for era in ERAS:
            base, head = era_shas(p, era)
            allowed, src, status = tc.era(repo_path, base, head)
            for s in src:
                src_tally[s] += 1
            if status == "undeclared":
                undeclared.append({"instance_id": p["instance_id"], "era": era})
                continue
            if status == "empty":
                # base and head disagree
                probed.append({"instance_id": p["instance_id"], "era": era,
                               **tc.describe(allowed)})
                allowed = tc.probe(allowed)
            per_repo[p["repo"]].append((allowed, p["instance_id"], era))

    # ---- cover per repo; keep old pins
    prior = load_prior(args.prev, tc)
    images, kept_sticky = [], 0
    for repo, entries in sorted(per_repo.items(), key=lambda kv: -len(kv[1])):
        pins = list(tc.cover([a for a, _, _ in entries]))
        for allowed, iid, era in entries:
            p = prior.get((iid, era))
            if p and tc.contains(allowed, p) and p not in pins:
                pins.append(p)
        pins.sort()
        assign: dict[tuple, tuple] = {}
        for allowed, iid, era in entries:
            p = prior.get((iid, era))
            if p and tc.contains(allowed, p):
                assign[(iid, era)] = p
                kept_sticky += 1
            else:
                # some pin always fits
                assign[(iid, era)] = next(q for q in pins if tc.contains(allowed, q))
        for pin in pins:
            members = [k for k, v in assign.items() if v == pin]
            if not members:
                continue          # no image needed
            # both `version` and the toolchain key
            images.append({"repo": repo, tc.key: tc.fmt(pin),
                           "toolchain": tc.key, "version": tc.fmt(pin),
                           "pr1_instances": sorted(i for i, e in members if e == "pr1"),
                           "pr2_instances": sorted(i for i, e in members if e == "pr2"),
                           "n_slots": len(members)})

    # chains whose eras use different images
    home = {}
    for img in images:
        tag = f"{img['repo']}-{tc.tag}{img['version']}"
        for iid in img["pr1_instances"]:
            home.setdefault(iid, {})["pr1"] = tag
        for iid in img["pr2_instances"]:
            home.setdefault(iid, {})["pr2"] = tag
    eras_split = sorted(iid for iid, h in home.items()
                        if h.get("pr1") and h.get("pr2") and h["pr1"] != h["pr2"])

    plan = {"max_tier": args.max_tier, "language": args.lang,
            "n_chains": len(rows), "n_images": len(images),
            "n_instance_images": sum(len(h) for h in home.values()),
            "images": images, "eras_split": eras_split,
            "anticipation_only": ant_only,
            "test_tree_relocated": relocated,
            "introducer_post_dates_report": impossible,
            "probed": probed,
            "unbuildable": {"undeclared": undeclared}}
    out_path.write_text(json.dumps(plan, indent=2) + "\n")

    if not args.no_master:
        print("master: " + write_plan_to_master(
            images, eras_split, tc, args.lang, args.max_tier, run_id,
            pathlib.Path(args.store) if args.store else None))

    print(f"chains (gradeable, tier<={args.max_tier}, {args.lang}): {len(rows)}")
    print(f"  skipped: " + ", ".join(f"{k}={v}" for k, v in skipped.most_common()))
    n_audited = len(introducer_guard.audited_ids() & seen)
    print(f"  SZZ date guard: {len(impossible)} dropped (blamed introducer post-dates "
          f"the bug report) of {n_audited} audited; "
          f"{len(seen) - n_audited} chains have no audit yet")
    if ant_only:
        print(f"  excluded (PR1 changed no test file -> anticipation-only, can never "
              f"be fully gradeable): {len(ant_only)}"
              f"  [--include-anticipation-only to build them]")
    if prior:
        print(f"  sticky: {kept_sticky} (chain, era) slots kept their existing "
              f"interpreter (recipes stay valid)")
    print(f"ENV IMAGES: {len(images)} across {len(per_repo)} repos")
    print(f"INSTANCE IMAGES: {plan['n_instance_images']} "
          f"(one per chain per era)\n")
    print(f"{'repo':14s} {tc.key:>7s} {'slots':>6s} {'pr1':>5s} {'pr2':>5s}")
    for img in images:
        print(f"{img['repo']:14s} {img['version']:>7s} {img['n_slots']:6d} "
              f"{len(img['pr1_instances']):5d} {len(img['pr2_instances']):5d}")
    print(f"\nchains whose two eras land in different images: {len(eras_split)}"
          f"  (normal under per-era planning, not a special case)")
    print(f"eras probed at hi (base/head of one PR disagree): {len(probed)}")
    print(f"eras undeclared (no version signal): {len(undeclared)}  "
          f"(reported, not silently dropped)")
    print(f"version sources seen: {dict(src_tally.most_common())}")
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
