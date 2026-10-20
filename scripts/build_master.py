#!/usr/bin/env python3
"""Build the master table from all mined chains."""
from __future__ import annotations

import collections
import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config  # noqa: E402
from foresight.filters import _instant  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_candidate_pool import recency_tier as _recency  # noqa: E402

ROOT = config.REPO_ROOT
CH = ROOT / "data" / "chains"
OUT = ROOT / "data" / "chains_master.jsonl"
TESTS_OUT = ROOT / "data" / "test_lists.jsonl"
REPORT = ROOT / "data" / "master_build_report.json"

JS_REPOS = {"vite", "vuecore", "axios", "svelte"}
OK_FULL = {"validated"}
# PR1 tests not measured
OK_PARTIAL = {"validated_ant_only", "validated_pr1_uncollectible"}
REJ_STATUS = {"pr2_not_fail_to_pass", "no_pr1_failure"}  # others: infra errors

# security chains, in their own dir
SECURITY_SRC = CH / "security" / "security_chains.jsonl"

MINING_FAMILY = {  # filename suffix -> source label
    "candidates": "git_text", "label_candidates": "label_szz",
    "changelog_candidates": "changelog_szz", "candidates_5y": "git_text",
    "delta": "git_text", "textmined": "git_text",
}


# introducer came from blame
_BLAME_EV = re.compile(r"SZZ|blame", re.I)


def iter_rows(path: Path):
    # missing input is fine
    if not path.exists():
        return
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def iid_of(r: dict) -> str | None:
    if r.get("instance_id"):
        return r["instance_id"]
    if r.get("repo") and r.get("earlier_pr") is not None:
        return f"{r['repo']}__{r['earlier_pr']}-{r['later_pr']}"
    return None


def load_keyed(path: Path) -> dict:
    out = {}
    for r in iter_rows(path):
        i = iid_of(r)
        if i:
            out[i] = r
    return out


def _interp(tag: str | None) -> str | None:
    """`<repo>-py3.12` -> `python3.12`, `<repo>-node18` -> `node18`."""
    if not tag:
        return None
    m = re.search(r"-(py|node)([0-9.]+)$", tag)
    if not m:
        return None
    return ("python" if m.group(1) == "py" else "node") + m.group(2)


def main() -> None:
    # one-time build; --force to redo
    if OUT.exists() and "--force" not in sys.argv:
        have = sum(1 for line in OUT.read_text().splitlines() if line.strip())
        sys.exit(f"{OUT} already exists with {have} rows.\n"
                 f"build_master.py is the one-time migration, not a rebuild step -- "
                 f"the stages write into the master now.\nRe-run with --force only if "
                 f"you mean to regenerate it from data/chains/ as it stands today.")
    rep = {"sources": {}, "problems": collections.defaultdict(list)}
    master: dict[str, dict] = {}

    # ---- 1. mining: identity + sources[] ------------------------------------
    for p in sorted(CH.glob("*.jsonl")):
        fam = p.name.split("_", 1)[1].replace(".jsonl", "") if "_" in p.name else ""
        src = MINING_FAMILY.get(fam)
        if not src or "validated" in p.name:
            continue
        n = 0
        for r in iter_rows(p):
            i = iid_of(r)
            if not i or r.get("earlier_pr") is None:
                continue
            n += 1
            row = master.setdefault(i, {"instance_id": i, "sources": []})
            row.setdefault("repo", r["repo"])
            row.setdefault("introducer_pr", r["earlier_pr"])
            row.setdefault("fixer_pr", r["later_pr"])
            for k in ("pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha",
                      "pr1_date", "pr2_date", "category"):
                if r.get(k) and not row.get(k):
                    row[k] = r[k]
            ev = {"source": src, "evidence": r.get("evidence", ""),
                  "evidence_locus": "commit_msg" if src == "git_text" else None,
                  "found_in": p.name}
            if not any(e["source"] == ev["source"] and e["evidence"] == ev["evidence"]
                       for e in row["sources"]):
                row["sources"].append(ev)
            if "date_inversion" in (r.get("flags") or []):
                row["date_inversion"] = True
        rep["sources"][p.name] = n

    # ---- 1b. security chains
    if SECURITY_SRC.exists():
        n = 0
        for r in iter_rows(SECURITY_SRC):
            i = iid_of(r)
            if not i:
                continue
            n += 1
            row = master.setdefault(i, {"instance_id": i, "sources": []})
            for k in ("repo", "language", "introducer_pr", "fixer_pr", "pr1_base_sha",
                      "pr1_head_sha", "pr2_base_sha", "pr2_head_sha", "pr1_date",
                      "pr2_date", "category", "security", "szz"):
                if r.get(k) is not None and row.get(k) is None:
                    row[k] = r[k]
            row["is_security_vulnerability"] = True
            for ev in r.get("sources", []):
                if ev not in row["sources"]:
                    row["sources"].append(ev)
        rep["sources"][SECURITY_SRC.name] = n

    # ---- 2. pool: language, tier, extras
    pool = load_keyed(CH / "candidate_pool.jsonl")
    rep["sources"]["candidate_pool.jsonl"] = len(pool)
    for i, r in pool.items():
        row = master.setdefault(i, {"instance_id": i, "sources": []})
        row.setdefault("repo", r["repo"])
        row.setdefault("introducer_pr", r["earlier_pr"])
        row.setdefault("fixer_pr", r["later_pr"])
        for k in ("pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha",
                  "pr1_date", "pr2_date", "category"):
            if r.get(k) and not row.get(k):
                row[k] = r[k]
        row["language"] = r.get("language")
        row["recency_tier"] = r.get("recency_tier")
        if not row["sources"]:
            row["sources"] = [{"source": "unknown", "evidence": r.get("evidence", ""),
                               "evidence_locus": None, "found_in": "candidate_pool.jsonl"}]
            rep["problems"]["source_unknown"].append(i)

    # ---- 3. stage-1a filters -------------------------------------------------
    stage = load_keyed(CH / "pool_stage1a.jsonl")
    rep["sources"]["pool_stage1a.jsonl"] = len(stage)

    # ---- 4. judge ------------------------------------------------------------
    judge = load_keyed(ROOT / "data" / "pool_judge.jsonl")
    rep["sources"]["pool_judge.jsonl"] = len(judge)
    # SZZ attribution verdicts
    attrib = load_keyed(ROOT / "data" / "szz_attribution_judge.jsonl")
    rep["sources"]["szz_attribution_judge.jsonl"] = len(attrib)
    v1 = json.load(open(ROOT / "data" / "judge_results.json"))
    v1keyed = {}
    for k, v in v1.items():
        if k == "_note":
            continue
        try:
            repo, prs = k.split("/", 1)
            a, b = prs.replace("PR", "").split("->")
            v1keyed[f"{repo}__{int(a)}-{int(b)}"] = v
        except Exception:
            rep["problems"]["v1_judge_key_unparsed"].append(k)
    rep["sources"]["judge_results.json (v1 3-judge)"] = len(v1keyed)

    # ---- 5. plan -------------------------------------------------------------
    plan = {}
    for pf in ("docker_plan.json", "docker_plan_javascript.json"):
        d = json.load(open(CH / pf))
        for im in d["images"]:
            for i in im.get("pr1_instances", []) + im.get("pr2_instances", []):
                plan.setdefault(i, im)
    rep["sources"]["docker_plan*.json"] = len(plan)

    # ---- 6. install recipes --------------------------------------------------
    def recipe(repo: str, sha: str | None):
        if not sha:
            return None
        p = ROOT / "docker" / "install" / repo / f"{sha[:12]}.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        if not isinstance(d, dict):
            # bad recipe: keep and flag
            rep["problems"]["recipe_not_an_object"].append(str(p))
            return {"commit": sha[:12], "install": [], "test_cmd": d,
                    "malformed": True}
        out = {"commit": sha[:12], "install": d.get("install", [])}
        for k in ("test_cmd", "python", "node", "pre_install", "env", "apt"):
            if d.get(k):
                out[k] = d[k]
        return out

    # ---- 7. validation (v2 docker runs, then v1 local) -----------------------
    execs: dict[str, tuple[str, dict]] = {}
    for p in sorted(glob.glob(str(ROOT / "data/docker_runs/*/validated_*.jsonl"))):
        run = Path(p).parent.name
        n = 0
        for r in iter_rows(Path(p)):
            i = iid_of(r)
            if i:
                execs[i] = (run, r)
                n += 1
        rep["sources"][f"docker_runs/{run}/{Path(p).name}"] = n
    v1val: dict[str, dict] = {}
    for p in sorted(CH.glob("*_validated*.jsonl")):
        n = 0
        for r in iter_rows(p):
            i = iid_of(r)
            if i:
                v1val.setdefault(i, r)
                n += 1
        rep["sources"][p.name] = n

    # ---- 8. verification campaigns (verify67 / image_verify / amd64_verify) --
    verifs: dict[str, list] = collections.defaultdict(list)
    for p in sorted(CH.glob("*_verify*.jsonl")):
        camp = p.name.split("_", 1)[1].replace(".jsonl", "")
        n = 0
        for r in iter_rows(p):
            i = iid_of(r)
            if i:
                verifs[i].append({"campaign": camp, "ok": r.get("ok"),
                                  "n_anticipation": r.get("n_antic"),
                                  "n_pr1_f2p": r.get("n_pr1"),
                                  "n_pr1_p2p": r.get("n_p2p"),
                                  "reasons": r.get("reasons") or []})
                n += 1
        rep["sources"][p.name] = n

    # ---- 9. published --------------------------------------------------------
    ds = load_keyed(ROOT / "data" / "foresight_dataset.jsonl")
    rep["sources"]["foresight_dataset.jsonl"] = len(ds)

    # validated chains with no mining row
    for i, (_run, r) in list(execs.items()) + [(k, ("v1-local", v)) for k, v in v1val.items()]:
        if i in master:
            continue
        try:
            a, b = i.split("__")[1].split("-")
        except Exception:
            rep["problems"]["unparseable_instance_id"].append(i)
            continue
        master[i] = {"instance_id": i, "repo": r.get("repo") or i.split("__")[0],
                     "introducer_pr": int(a), "fixer_pr": int(b),
                     "sources": [{"source": "unknown", "evidence": "",
                                  "evidence_locus": None,
                                  "found_in": "validation record only"}]}
        for k in ("pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha",
                  "category"):
            if r.get(k):
                master[i][k] = r[k]
        rep["problems"]["row_created_from_validation_only"].append(i)

    def jload(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []
        return v or []

    # ---- assemble ------------------------------------------------------------
    p2p_rows = []
    for i, row in master.items():
        repo = row.get("repo") or i.split("__")[0]
        row["repo"] = repo
        row.setdefault("language", "javascript" if repo in JS_REPOS else "python")
        d = ds.get(i)
        if d:                                    # published rows
            row.setdefault("pr1_base_sha", d.get("base_commit"))
            for k in ("pr1_head_sha", "pr2_base_sha", "pr2_head_sha"):
                row.setdefault(k, d.get(k))
        for k in ("pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha",
                  "pr1_date", "pr2_date", "category"):
            row.setdefault(k, None)
        if not row.get("pr1_base_sha"):
            rep["problems"]["missing_pr1_base_sha"].append(i)

        # check evidence, not source name
        blame = any(_BLAME_EV.search(s.get("evidence") or "") or
                    str(s.get("source", "")).endswith("szz")
                    for s in row["sources"])
        row["introducer_found_by"] = "git_blame" if blame else "human_reference"
        row["szz"] = ({"candidates": [{"pr": row["introducer_pr"], "blamed_lines": None,
                                       "rank": 1}],
                       "chosen_rank": 1, "blame_agreement": None, "blamed_files": None}
                      if blame else None)
        # attribution verdict; only A keeps
        a = attrib.get(i)
        if row["szz"] is not None and a:
            row["szz"]["attribution"] = {
                "code": a.get("code"),
                "holds": a.get("code") == "A" if a.get("code") else None,
                "reasoning": a.get("reasoning"),
                "skipped": a.get("skipped"),
                "error": a.get("error"),
            }
        elif row["szz"] is not None:
            row["szz"]["attribution"] = None

        s = stage.get(i, {})
        # tier computed if missing
        row["filters"] = {
            "recency_tier": (row.pop("recency_tier", None)
                             or _recency(row.get("pr1_date"), row.get("pr2_date"))),
            "pr1_has_test_changes": s.get("pr1_has_test_changes"),
            "pr1_n_test_files": s.get("pr1_n_test_files"),
            "pr1_n_src_files": s.get("pr1_n_src_files"),
            "pr2_has_test_changes": s.get("pr2_has_test_changes"),
            "pr2_n_test_files": s.get("pr2_n_test_files"),
            "pr2_n_src_files": s.get("pr2_n_src_files"),
        }

        j = judge.get(i)
        if j and (j.get("eligibility") or j.get("skip")):
            row["judge"] = {
                "outcome": "ok",
                "verdicts": [{"model": "deepseek-v4-flash",
                              "eligibility": j.get("eligibility"),
                              "validity": j.get("validity"),
                              "impact": j.get("impact"), "reach": j.get("reach"),
                              "detectability": j.get("detectability"),
                              "severity": j.get("severity"),
                              "security": j.get("security"),
                              "mechanism": None,
                              "rationale": j.get("filter_reason")}],
                "consensus": {"eligibility": j.get("eligibility"),
                              "validity": j.get("validity"),
                              "n": 1, "n_agree": 1, "method": "single"},
                "gradeable": bool((j.get("derived") or {}).get("gradeable"))}
        elif j:
            row["judge"] = {"outcome": "error", "retryable": True,
                            "error": {"detail": j.get("error") or "no verdict returned"},
                            "verdicts": [], "consensus": None, "gradeable": False}
        elif i in v1keyed:
            labs = v1keyed[i]["labels"]
            row["judge"] = {
                "outcome": "ok",
                "verdicts": [{"model": f"unknown-subagent-{n}", "eligibility": lab,
                              "validity": None, "impact": None, "reach": None,
                              "detectability": None, "severity": None,
                              "security": None, "mechanism": None, "rationale": None}
                             for n, lab in enumerate(labs, 1)],
                "consensus": {"eligibility": v1keyed[i]["consensus"], "validity": None,
                              "n": len(labs),
                              "n_agree": max(collections.Counter(labs).values()),
                              "method": "majority"},
                "gradeable": v1keyed[i]["consensus"] == "missed_edge_case"}
        else:
            row["judge"] = None

        ev = execs.get(i)
        vv = v1val.get(i)
        # recipe key: the built commit
        ss = ((ev[1] if ev else (vv or {})).get("state_shas") or {})
        built1 = (ss.get("pr1_base") or {}).get("code") or row.get("pr1_base_sha")
        built2 = (ss.get("pr2_head") or {}).get("code") or row.get("pr2_head_sha")
        # the image that ran beats the plan
        im = plan.get(i)
        used = (ev[1].get("images") if ev else None) or {}
        if used:
            def era(*keys):
                for k in keys:
                    if used.get(k):
                        return used[k]
                return None
            # interpreter parsed from the tag
            row["plan"] = {
                "outcome": "planned", "provenance": "from_validation",
                "run_id": ev[0],
                "eras_split": bool(ev[1].get("split_image")),
                "pr1": {"interpreter": _interp(era("pr1_base", "pr1_head")),
                        "env_image": era("pr1_base", "pr1_head"),
                        "build_sha": (built1 or "")[:12] or None},
                "pr2": {"interpreter": _interp(era("pr2_head", "pr2_base")),
                        "env_image": era("pr2_head", "pr2_base"),
                        "build_sha": (built2 or "")[:12] or None}}
        elif im:
            tool = "node" if im.get("toolchain") == "node" else "python"
            ver = im.get("version") or im.get("python")
            tag = f"foresight:env-{repo}-{'node' if tool == 'node' else 'py'}{ver}"
            row["plan"] = {
                "outcome": "planned", "provenance": "current_plan",
                "run_id": "plan-2026-08-16", "eras_split": None,
                "pr1": {"interpreter": f"{tool}{ver}", "env_image": tag,
                        "build_sha": (row.get("pr1_base_sha") or "")[:12] or None},
                "pr2": {"interpreter": f"{tool}{ver}", "env_image": tag,
                        "build_sha": (row.get("pr2_head_sha") or "")[:12] or None}}
        elif ev or vv:
            # validated before docker: no image
            row["plan"] = {"outcome": "not_applicable",
                           "reason": "validated_pre_docker",
                           "provenance": "from_validation", "run_id": ev[0] if ev else "v1-local"}
        else:
            row["plan"] = None

        r1 = recipe(repo, built1)
        r2 = recipe(repo, built2)
        usable = bool(r1 and r2 and not r1.get("malformed") and not r2.get("malformed"))
        row["install"] = {"outcome": "ok" if usable else None,   # set below
                          "pr1_base": r1, "pr2_head": r2}

        if ev or vv:
            run, r = ev if ev else ("v1-local", vv)
            st = r.get("status")
            outcome = ("ok_full" if st in OK_FULL else
                       "ok_partial" if st in OK_PARTIAL else
                       "rejected" if st in REJ_STATUS else "error")
            ant = jload(r.get("anticipation_tests"))
            f2p = jload(r.get("pr1_f2p_tests"))
            p2p = jload(r.get("pr1_p2p_tests"))
            row["validation"] = {
                "outcome": outcome, "status": st,
                "run_id": run,
                "image_digest": None,
                "anticipation_tests": ant,
                "n_anticipation": len(ant), "n_pr1_f2p": len(f2p), "n_pr1_p2p": len(p2p),
                "test_lists_ref": i if (f2p or p2p) else None,
                "state_shas": r.get("state_shas"), "outcomes": r.get("n_outcomes"),
                "anticipation_failure_class": None,
            }
            if outcome == "error":
                row["validation"]["error"] = {
                    "detail": st, "missing_recipes": r.get("missing_recipes") or []}
            if f2p or p2p:
                p2p_rows.append({"instance_id": i, "pr1_f2p_tests": f2p,
                                 "pr1_p2p_tests": p2p})
        else:
            row["validation"] = None

        # --- gates, in pipeline order
        j = row.get("judge")
        f = row["filters"]
        if row.get("excluded"):
            gate = "excluded"
        elif not j:
            gate = "not_judged"
        elif not j.get("gradeable"):
            gate = "not_gradeable"
        elif f.get("recency_tier") is None or f["recency_tier"] > 5:
            gate = "tier_above_max"
        elif f.get("pr2_has_test_changes") is False:
            gate = "no_pr2_test_changes"
        elif f.get("pr1_has_test_changes") is False:
            gate = "anticipation_only"
        elif row["language"] not in ("python", "javascript", "go"):
            # no runner for this language
            gate = "no_runner"
        else:
            gate = None

        if row["plan"] is None:
            row["plan"] = ({"outcome": "pending"} if gate is None else
                           {"outcome": "not_applicable", "reason": gate})
        elif row["plan"].get("outcome") == "planned":
            row["plan"]["outcome"] = "ok"

        planned_ok = row["plan"].get("outcome") == "ok"
        if row["install"]["outcome"] != "ok":
            row["install"] = {**row["install"],
                              "outcome": "pending" if planned_ok else "not_applicable",
                              **({} if planned_ok else
                                 {"reason": gate or "not_planned"})}
        if row["validation"] is None:
            row["validation"] = (
                {"outcome": "pending"} if row["install"]["outcome"] == "ok" else
                {"outcome": "not_applicable",
                 "reason": ("no_install_recipe" if planned_ok else (gate or "not_planned"))})

        row["verifications"] = verifs.get(i) or None
        row["published"] = ({"version": "v1-67",
                             "pr2_spec_code": d.get("pr2_spec_code")} if d else None)
        a, b = _instant(row.get("pr1_date")), _instant(row.get("pr2_date"))
        row["date_inversion"] = bool(a and b and a > b)
        if row["date_inversion"]:
            row["excluded"] = {"reason": "date_inversion",
                               "detail": f"introducer merged after fixer "
                                         f"({row['pr1_date']} > {row['pr2_date']})",
                               "by": "build_master"}
        elif row["introducer_pr"] and row["introducer_pr"] == row["fixer_pr"]:
            row["excluded"] = {"reason": "self_chain",
                               "detail": "a PR cannot introduce the bug it fixes",
                               "by": "build_master"}
        else:
            row["excluded"] = None

    ordered = [master[k] for k in sorted(master)]
    OUT.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in ordered))
    TESTS_OUT.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                                 for r in sorted(p2p_rows, key=lambda r: r["instance_id"])))
    rep["problems"] = {k: v for k, v in rep["problems"].items()}
    rep["totals"] = {
        "rows": len(ordered),
        "with_judge": sum(1 for r in ordered if r["judge"]),
        "gradeable": sum(1 for r in ordered if (r["judge"] or {}).get("gradeable")),
        "planned": sum(1 for r in ordered if r["plan"]["outcome"] == "ok"),
        "installed": sum(1 for r in ordered if r["install"]["outcome"] == "ok"),
        "validated_any": sum(1 for r in ordered if (r["validation"] or {}).get("outcome")
                            in ("ok_full", "ok_partial", "rejected", "error")),
        "install_pending": sum(1 for r in ordered
                               if r["install"]["outcome"] == "pending"),
        "validation_pending": sum(1 for r in ordered
                                  if (r["validation"] or {}).get("outcome") == "pending"),
        "validation_ok_full": sum(1 for r in ordered
                                  if (r["validation"] or {}).get("outcome") == "ok_full"),
        "validation_ok_partial": sum(1 for r in ordered
                                     if (r["validation"] or {}).get("outcome") == "ok_partial"),
        "excluded": sum(1 for r in ordered if r["excluded"]),
        "published": sum(1 for r in ordered if r["published"]),
        "test_list_rows": len(p2p_rows),
        "master_mb": round(OUT.stat().st_size / 1e6, 1),
        "test_lists_mb": round(TESTS_OUT.stat().st_size / 1e6, 1),
    }
    REPORT.write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep["totals"], indent=1))
    print("problems:", {k: len(v) for k, v in rep["problems"].items()})


if __name__ == "__main__":
    main()
