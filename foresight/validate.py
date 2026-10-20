"""Validate chains by running PR2's tests in three states."""

from __future__ import annotations

from pathlib import Path

from .execution.runner import (
    ERROR,
    FAILED,
    PASSED,
    changed_test_files,
    run_tests,
)


def _keys(d):
    return [list(k) for k in d]


def _fail_to_pass(repo: Path, base: str, head: str, test_files: list[str], tag: str, **run_kw):
    """Tests failing at base, passing at head."""
    r_base = run_tests(repo, base, test_files, label=f"{tag}base", overlay_from=head, **run_kw)
    r_head = run_tests(repo, head, test_files, label=f"{tag}head", **run_kw)
    if not (r_base.ran and r_head.ran):
        return None, r_base, r_head
    f2p = {
        k
        for k in r_head.outcomes
        if r_head.outcomes.get(k) == PASSED and r_base.outcomes.get(k) in (FAILED, ERROR)
    }
    return f2p, r_base, r_head


def validate_chain(repo: Path, repo_name: str, chain: dict, **run_kw) -> dict:
    pr1_head = chain["pr1_head_sha"]
    pr2_base = chain["pr2_base_sha"]
    pr2_head = chain["pr2_head_sha"]

    out = {
        "repo": repo_name,
        "earlier_pr": chain["earlier_pr"],
        "later_pr": chain["later_pr"],
        "category": chain["category"],
        "evidence": chain["evidence"],
        "pr1_base_sha": chain["pr1_base_sha"],
        "pr1_head_sha": pr1_head,
        "pr2_base_sha": pr2_base,
        "pr2_head_sha": pr2_head,
        "status": "",
        "test_files": [],
        "p2f_tests": [],
        "anticipation_tests": [],
        "anticipation_tests_incl_error": [],
        "pr1_test_files": [],
        "pr1_f2p_tests": [],
        "notes": {},
    }

    tfiles = changed_test_files(repo, pr2_base, pr2_head)
    out["test_files"] = tfiles
    if not tfiles:
        out["status"] = "no_test_changes"
        return out

    p2f, r_base, r_head = _fail_to_pass(repo, pr2_base, pr2_head, tfiles, "pr2", **run_kw)
    if p2f is None:
        out["status"] = "env_failed_pr2"
        out["notes"] = {"pr2base": r_base.note, "pr2head": r_head.note}
        return out

    out["p2f_tests"] = _keys(sorted(p2f))
    if not p2f:
        out["status"] = "pr2_not_fail_to_pass"
        return out

    if pr1_head == pr2_base:
        r_pr1 = r_base  # adjacent chain: reuse
    else:
        r_pr1 = run_tests(repo, pr1_head, tfiles, label="pr1head", overlay_from=pr2_head, **run_kw)
    if not r_pr1.ran:
        out["status"] = "env_failed_pr1"
        out["notes"] = {"pr1head": r_pr1.note}
        return out

    clean = sorted(k for k in p2f if r_pr1.outcomes.get(k) == FAILED)
    incl_error = sorted(k for k in p2f if r_pr1.outcomes.get(k) in (FAILED, ERROR))
    out["anticipation_tests"] = _keys(clean)
    out["anticipation_tests_incl_error"] = _keys(incl_error)
    out["status"] = "validated" if clean else "no_pr1_failure"

    # PR1's own fail->pass tests
    if clean:
        pr1_tfiles = changed_test_files(repo, chain["pr1_base_sha"], pr1_head)
        out["pr1_test_files"] = pr1_tfiles
        if pr1_tfiles:
            pr1_f2p, _, _ = _fail_to_pass(
                repo, chain["pr1_base_sha"], pr1_head, pr1_tfiles, "pr1own", **run_kw
            )
            if pr1_f2p:
                out["pr1_f2p_tests"] = _keys(sorted(pr1_f2p))
    return out
