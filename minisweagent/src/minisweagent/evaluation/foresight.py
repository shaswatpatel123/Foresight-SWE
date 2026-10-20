#!/usr/bin/env python3
"""Grade a patch: PR1 tests and anticipation tests."""

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

_JUNIT_MARKER = "<<<FORESIGHT_JUNIT>>>"
_PYTEST_HELPER = "run_state.sh"
_DJANGO_HELPER = "run_state_django.sh"
# Test runner script for each language.
_RUNNER_HELPERS = {
    "pytest": _PYTEST_HELPER,
    "django": _DJANGO_HELPER,
    "node": "run_state_node.sh",
    "go": "run_state_go.sh",
}


def parse_junit(xml_text: str) -> dict[str, str]:
    """JUnit XML -> {classname::name: status}."""
    results: dict[str, str] = {}
    if not xml_text or not xml_text.strip():
        return results
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return results
    for tc in root.iter("testcase"):
        classname = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        tid = f"{classname}::{name}" if classname else name
        if tc.find("failure") is not None:
            status = "FAILED"
        elif tc.find("error") is not None:
            status = "ERROR"
        elif tc.find("skipped") is not None:
            status = "SKIPPED"
        else:
            status = "PASSED"
        results[tid] = status
    return results


def _lookup(results: dict[str, str], test_id: str) -> str:
    """Test status, tolerant of param ids."""
    if test_id in results:
        return results[test_id]
    base = test_id.split("[", 1)[0]
    for k, v in results.items():
        if k.split("[", 1)[0] == base:
            return v
    # name-only fallback (last ::-segment)
    want = test_id.split("::")[-1].split("[", 1)[0]
    for k, v in results.items():
        if k.split("::")[-1].split("[", 1)[0] == want:
            return v
    return "MISSING"


def _score(results: dict[str, str], test_ids: list[str] | str) -> dict:
    # decode a JSON-string list if needed
    if isinstance(test_ids, str):
        try:
            test_ids = json.loads(test_ids)
        except json.JSONDecodeError:
            test_ids = [test_ids] if test_ids else []
    test_ids = list(test_ids or [])
    per_test = {tid: _lookup(results, tid) for tid in test_ids}
    passed = sum(1 for s in per_test.values() if s == "PASSED")
    total = len(test_ids)
    pct = (passed / total * 100.0) if total else 0.0
    return {"total": total, "passed": passed, "pct": round(pct, 2), "per_test": per_test}


# Timeout grows with test file count.
_PER_FILE_TIMEOUT = 2400
_MAX_OVERLAY_TIMEOUT = 14400


def _overlay_timeout(n_files: int) -> int:
    return max(1800, min(_MAX_OVERLAY_TIMEOUT, _PER_FILE_TIMEOUT + 300 * max(0, n_files - 1)))


def _run_overlay(env, checkout_sha: str, overlay_sha: str, test_files: list[str],
                 out_xml: str, helper: str, timeout: int | None = None,
                 recipe_shas: list[str] | None = None):
    """Run tests in the image; return JUnit XML."""
    files = " ".join(test_files)
    ov = overlay_sha if overlay_sha else "-"
    pick = ""
    if cands := " ".join(s for s in (recipe_shas or []) if s):
        # prefer a recipe that has test_cmd
        _has = (
            'python3 -c "import json,sys;c=json.load(open(sys.argv[1]));'
            'sys.exit(0 if (c.get(\'test_cmd\') or \'\') else 1)" "$_f" 2>/dev/null'
        )
        pick = (
            f'for _s in {cands}; do '
            f'_f="${{INSTALL_DIR:-/opt/install}}/$(echo "$_s" | cut -c1-12).json"; '
            f'if [ -f "$_f" ] && {_has}; then export FORESIGHT_RECIPE_SHA="$_s"; break; fi; done; '
            f'if [ -z "${{FORESIGHT_RECIPE_SHA:-}}" ]; then for _s in {cands}; do '
            f'_f="${{INSTALL_DIR:-/opt/install}}/$(echo "$_s" | cut -c1-12).json"; '
            f'if [ -f "$_f" ]; then export FORESIGHT_RECIPE_SHA="$_s"; break; fi; done; fi; '
            f'echo "recipe_sha=${{FORESIGHT_RECIPE_SHA:-<none found>}}"; '
        )
    cmd = (
        f"cd {env.config.cwd} && rm -f {out_xml}; {pick}"
        f"{helper} {checkout_sha} {ov} {out_xml} {files}; "
        f"echo '{_JUNIT_MARKER}'; cat {out_xml} 2>/dev/null || true"
    )
    if timeout is None:
        timeout = _overlay_timeout(len(test_files))
    out, timed_out, _, rc = env.exec_run_with_timeout(cmd, timeout=timeout)
    # decode bytes from a timed-out run
    if isinstance(out, (bytes, bytearray)):
        out = out.decode("utf-8", errors="replace")
    xml_text = out.split(_JUNIT_MARKER, 1)[1].strip() if _JUNIT_MARKER in out else ""
    return xml_text, out, timed_out


def _refresh_helpers(env) -> list[str]:
    """Copy repo's docker/ helpers into the sandbox."""
    # on by default; FORESIGHT_HELPER_DIR="" disables
    env_dir = os.environ.get("FORESIGHT_HELPER_DIR")
    if env_dir is None:
        default = Path(__file__).resolve().parents[4] / "docker"
        src_dir = str(default) if default.is_dir() else ""
    else:
        src_dir = env_dir
    sandbox = getattr(env, "sandbox_dir", None)
    if not src_dir or sandbox is None:
        return []
    dest_dir = Path(sandbox) / "usr" / "local" / "bin"
    done = []
    for name in ("run_state.sh", "run_state_django.sh", "run_state_node.sh",
                 "run_state_go.sh", "node_run_tests.sh",
                 "go_run_tests.sh", "node_to_junit.py", "go_to_junit.py",
                 "django_to_junit.py"):
        src, dst = Path(src_dir) / name, dest_dir / name
        if not src.is_file() or not dst.is_file():
            continue                       # not in the repo, or not in this image
        body = src.read_bytes()
        if body == dst.read_bytes():
            continue                       # already current
        dst.write_bytes(body)
        dst.chmod(0o755)
        done.append(name)
    return done


def evaluate_foresight(instance: dict, instance_dir: Path, env,
                       grade_mode: str = "pr1") -> str:
    """Grade the agent's patch and write the report.

    pr1: F2P + P2P + anticipation.
    pr2: anticipation is F2P; one run.
    """
    if grade_mode not in ("pr1", "pr2"):
        raise ValueError(f"grade_mode must be 'pr1' or 'pr2', got {grade_mode!r}")
    instance_dir.mkdir(parents=True, exist_ok=True)
    instance_id = instance["instance_id"]
    runner = instance.get("runner", "pytest")
    helper = _RUNNER_HELPERS.get(runner, _PYTEST_HELPER)
    if refreshed := _refresh_helpers(env):
        print(f"[{instance_id}] refreshed in-image helpers: {', '.join(refreshed)}")

    # 1. Commit the agent's tree to get a stable SHA the helper can check out.
    commit_cmd = (
        f"cd {env.config.cwd} && git config user.email 'foresight@eval' "
        f"&& git config user.name 'foresight' && git add -A "
        f"&& git commit -m foresight-agent-solution --no-verify >/dev/null 2>&1; "
        f"git rev-parse HEAD"
    )
    out, timed_out, _, rc = env.exec_run_with_timeout(commit_cmd, timeout=300)
    agent_sha = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if rc != 0 or len(agent_sha) < 7:
        report = {instance_id: {
            "instance_id": instance_id, "repo": instance.get("repo"), "runner": runner,
            "error": "commit_failed", "commit_output": out, "resolved": False,
        }}
        (instance_dir / "foresight_report.json").write_text(json.dumps(report, indent=2))
        return "commit_failed"

    # unique paths: /tmp is shared
    safe = instance_id.replace("/", "_")
    pr1_out = f"/foresight_{safe}_pr1.xml"
    ant_out = f"/foresight_{safe}_ant.xml"

    # 2. PR1 tests (skipped in pr2 mode)
    if grade_mode == "pr2":
        pr1_xml, pr1_log, pr1_to = "", "(pr1 overlay skipped: grade_mode=pr2)", False
    else:
        pr1_xml, pr1_log, pr1_to = _run_overlay(
            env, agent_sha, instance["pr1_head_sha"], instance["pr1_test_files"],
            pr1_out, helper,
            recipe_shas=[instance.get("pr1_head_sha"), instance.get("base_commit")])
    # 3. Anticipation tests (overlay from pr2_head).
    ant_xml, ant_log, ant_to = _run_overlay(
        env, agent_sha, instance["pr2_head_sha"], instance["anticipation_test_files"],
        ant_out, helper,
        recipe_shas=[instance.get("pr2_head_sha"), instance.get("base_commit")])

    (instance_dir / "pr1_junit.xml").write_text(pr1_xml)
    (instance_dir / "ant_junit.xml").write_text(ant_xml)
    (instance_dir / "test_output.txt").write_text(
        f"=== PR1 ({helper}) ===\n{pr1_log}\n\n=== ANTICIPATION ===\n{ant_log}")

    ant_results = parse_junit(ant_xml)
    ant = _score(ant_results, instance["ANTICIPATION_FAIL_TO_PASS"])
    if grade_mode == "pr2":
        # F2P is anticipation; P2P from same JUnit
        pr1 = ant
        p2p = _score(ant_results, instance.get("PR2_PASS_TO_PASS", []))
    else:
        pr1_results = parse_junit(pr1_xml)
        pr1 = _score(pr1_results, instance["FAIL_TO_PASS"])
        # P2P from the same JUnit
        p2p = _score(pr1_results, instance.get("PASS_TO_PASS", []))
    gap = round(pr1["pct"] - ant["pct"], 2)

    # empty JUnit means no run (not a real 0)
    empty_pr1_junit = grade_mode != "pr2" and not pr1_xml.strip()
    empty_ant_junit = not ant_xml.strip()

    # resolved = F2P and P2P all pass
    p2p_ok = p2p["total"] == 0 or p2p["pct"] == 100.0
    pr1_resolved = pr1["pct"] == 100.0
    resolved = pr1_resolved and p2p_ok

    report = {instance_id: {
        "instance_id": instance_id,
        "repo": instance.get("repo"),
        "runner": runner,
        "agent_sha": agent_sha,
        # which tests defined success
        "grade_mode": grade_mode,
        "pr1": pr1,
        "p2p": p2p,
        "anticipation": ant,
        "gap": gap,
        "pr1_resolved": pr1_resolved,        # F2P only
        "no_regressions": p2p_ok,            # P2P all pass (or none)
        "anticipated": ant["pct"] == 100.0,
        "resolved": resolved,                # SWE-bench bar: F2P==100 AND P2P==100
        "timed_out": bool(pr1_to or ant_to),
        "empty_pr1_junit": empty_pr1_junit,  # spurious-zero flags (test run produced no XML)
        "empty_ant_junit": empty_ant_junit,
    }}
    (instance_dir / "foresight_report.json").write_text(json.dumps(report, indent=2))
    # separate status for an empty JUnit
    if empty_pr1_junit or empty_ant_junit:
        return "+".join(t for t, f in (("EMPTY_PR1_JUNIT", empty_pr1_junit),
                                        ("EMPTY_ANT_JUNIT", empty_ant_junit)) if f)
    return f"PR1={pr1['pct']}% ANT={ant['pct']}% gap={gap}%"


def reanticipate_only(instance: dict, instance_dir: Path, env) -> str:
    """Re-run only anticipation tests; update report."""
    instance_id = instance["instance_id"]
    report_path = instance_dir / "foresight_report.json"
    if not report_path.exists():
        return evaluate_foresight(instance, instance_dir, env)
    try:
        prior = json.loads(report_path.read_text()).get(instance_id, {})
    except (json.JSONDecodeError, OSError):
        prior = {}
    # Need a valid PR1 block to preserve; otherwise fall back to a full grade.
    if not prior.get("pr1") or prior.get("empty_pr1_junit"):
        return evaluate_foresight(instance, instance_dir, env)

    runner = instance.get("runner", "pytest")
    helper = _RUNNER_HELPERS.get(runner, _PYTEST_HELPER)

    # 1. commit the agent's tree
    commit_cmd = (
        f"cd {env.config.cwd} && git config user.email 'foresight@eval' "
        f"&& git config user.name 'foresight' && git add -A "
        f"&& git commit -m foresight-agent-solution --no-verify >/dev/null 2>&1; "
        f"git rev-parse HEAD"
    )
    out, timed_out, _, rc = env.exec_run_with_timeout(commit_cmd, timeout=300)
    agent_sha = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if rc != 0 or len(agent_sha) < 7:
        return "commit_failed"

    # 2. anticipation tests only
    safe = instance_id.replace("/", "_")
    ant_out = f"/foresight_{safe}_ant.xml"
    ant_xml, ant_log, ant_to = _run_overlay(
        env, agent_sha, instance["pr2_head_sha"], instance["anticipation_test_files"],
        ant_out, helper,
        recipe_shas=[instance.get("pr2_head_sha"), instance.get("base_commit")])
    (instance_dir / "ant_junit.xml").write_text(ant_xml)
    # Preserve the original PR1 log; replace only the ANTICIPATION section.
    try:
        old = (instance_dir / "test_output.txt").read_text()
        pr1_section = old.split("\n\n=== ANTICIPATION", 1)[0]
    except OSError:
        pr1_section = ""
    (instance_dir / "test_output.txt").write_text(
        f"{pr1_section}\n\n=== ANTICIPATION (heal-only rerun) ===\n{ant_log}")

    ant = _score(parse_junit(ant_xml), instance["ANTICIPATION_FAIL_TO_PASS"])
    empty_ant_junit = not ant_xml.strip()

    # Patch ONLY the anticipation-derived fields; keep PR1/P2P/resolution as-is.
    updated = dict(prior)
    updated["agent_sha"] = agent_sha
    updated["anticipation"] = ant
    updated["anticipated"] = ant["pct"] == 100.0
    updated["gap"] = round(prior["pr1"]["pct"] - ant["pct"], 2)
    updated["empty_ant_junit"] = empty_ant_junit
    updated["timed_out"] = bool(ant_to)  # PR1 not re-run; this reflects the ANT rerun only
    report_path.write_text(json.dumps({instance_id: updated}, indent=2))

    if empty_ant_junit:
        return "EMPTY_ANT_JUNIT"
    return f"HEAL ANT={ant['pct']}% gap={updated['gap']}%"


def make_foresight_report(predictions: dict, full_dataset: list, output_path: Path) -> Path:
    """Aggregate per-instance foresight_report.json into the gap headline metric."""
    per = []
    repo_acc: dict[str, list] = {}
    micro_pr1_pass = micro_pr1_tot = micro_ant_pass = micro_ant_tot = 0
    micro_p2p_pass = micro_p2p_tot = 0

    for inst in full_dataset:
        iid = inst["instance_id"]
        if iid not in predictions:
            continue
        rf = output_path / iid / "foresight_report.json"
        if not rf.exists():
            continue
        r = json.loads(rf.read_text()).get(iid, {})
        if "pr1" not in r:
            continue
        r.setdefault("p2p", {"total": 0, "passed": 0, "pct": 0.0})  # back-compat
        r.setdefault("no_regressions", r["p2p"]["total"] == 0 or r["p2p"]["pct"] == 100.0)
        per.append(r)
        repo_acc.setdefault(r.get("repo", "?"), []).append(r)
        micro_pr1_pass += r["pr1"]["passed"]; micro_pr1_tot += r["pr1"]["total"]
        micro_ant_pass += r["anticipation"]["passed"]; micro_ant_tot += r["anticipation"]["total"]
        micro_p2p_pass += r["p2p"]["passed"]; micro_p2p_tot += r["p2p"]["total"]

    n = len(per)

    def _mean(key_path):
        if not n:
            return 0.0
        vals = []
        for r in per:
            v = r
            for k in key_path:
                v = v[k]
            vals.append(v)
        return round(sum(vals) / n, 2)

    def _pct(a, b):
        return round(a / b * 100.0, 2) if b else 0.0

    by_repo = {}
    for repo, rs in sorted(repo_acc.items()):
        m = len(rs)
        by_repo[repo] = {
            "n": m,
            "mean_pr1_f2p": round(sum(x["pr1"]["pct"] for x in rs) / m, 2),
            "mean_p2p": round(sum(x["p2p"]["pct"] for x in rs) / m, 2),
            "mean_anticipation_f2p": round(sum(x["anticipation"]["pct"] for x in rs) / m, 2),
            "mean_gap": round(sum(x["gap"] for x in rs) / m, 2),
            "pr1_resolved": sum(1 for x in rs if x["pr1_resolved"]),
            "resolved": sum(1 for x in rs if x.get("resolved")),
            "anticipated": sum(1 for x in rs if x["anticipated"]),
        }

    report = {
        "graded_instances": n,
        "mean_pr1_f2p": _mean(["pr1", "pct"]),
        "mean_p2p": _mean(["p2p", "pct"]),
        "mean_anticipation_f2p": _mean(["anticipation", "pct"]),
        "mean_gap": _mean(["gap"]),
        "micro_pr1_f2p": _pct(micro_pr1_pass, micro_pr1_tot),
        "micro_p2p": _pct(micro_p2p_pass, micro_p2p_tot),
        "micro_anticipation_f2p": _pct(micro_ant_pass, micro_ant_tot),
        "micro_gap": round(_pct(micro_pr1_pass, micro_pr1_tot) - _pct(micro_ant_pass, micro_ant_tot), 2),
        "pr1_resolved_instances": sum(1 for r in per if r["pr1_resolved"]),
        "resolved_instances": sum(1 for r in per if r.get("resolved")),
        "anticipated_instances": sum(1 for r in per if r["anticipated"]),
        "by_repo": by_repo,
        "schema_version": 2,
    }
    print(f"Graded: {n}")
    print(f"Mean PR1 F2P:           {report['mean_pr1_f2p']}%")
    print(f"Mean P2P (regression):  {report['mean_p2p']}%")
    print(f"Mean Anticipation F2P:  {report['mean_anticipation_f2p']}%")
    print(f"Mean anticipation gap:  {report['mean_gap']}%")
    print(f"PR1 resolved (F2P):     {report['pr1_resolved_instances']}/{n}")
    print(f"Resolved (F2P & P2P):   {report['resolved_instances']}/{n}")
    print(f"Anticipated edge case:  {report['anticipated_instances']}/{n}")

    out = output_path / "final_foresight_report.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"Report written to {out}")
    return out
