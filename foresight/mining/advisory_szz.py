"""Security chains: advisory fix, then SZZ introducer."""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import changelog_szz
from ..langspec import get_spec

_PR_ANCHOR = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)", re.M)
_TRACKER = re.compile(r"\b(?:SECURITY|CVE-\d{4})-(\d+)|^Fixed #(\d+)", re.M)
_CVE_NUM = re.compile(r"CVE-(\d{4})-(\d+)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, errors="replace").stdout


def _exists(repo: Path, sha: str) -> bool:
    return bool(_git(repo, "cat-file", "-t", sha).strip() == "commit")


def _subject(repo: Path, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%s", sha).strip()


def _message(repo: Path, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%B", sha)


def _date(repo: Path, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%cI", sha).strip()


def _rev(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", rev).strip()


@dataclass
class SecurityChain:
    """One chain, in master row shape."""
    instance_id: str
    repo: str
    language: str
    introducer_pr: int
    fixer_pr: int
    pr1_base_sha: str
    pr1_head_sha: str
    pr2_base_sha: str
    pr2_head_sha: str
    pr1_date: str
    pr2_date: str
    category: str = "security_advisory"
    is_security_vulnerability: bool = True
    security: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    szz: dict = field(default_factory=dict)
    filters: dict = field(default_factory=dict)


def default_branch_commits(repo: Path) -> set[str]:
    """Commits on the default branch."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    out = _git(repo, "rev-list", head)
    return set(out.split())


def dedupe_cherry_picks(repo: Path, shas: list[str],
                        on_default: set[str] | None = None) -> list[str]:
    """One commit per fix; prefer the default branch."""
    by_patch: dict[str, list[str]] = {}
    for sha in shas:
        if not _exists(repo, sha):
            continue
        full = _rev(repo, sha)
        diff = _git(repo, "diff-tree", "-p", "--no-commit-id", full)
        pid = subprocess.run(["git", "patch-id", "--stable"], input=diff,
                             capture_output=True, text=True).stdout.split(" ")[0] or full
        by_patch.setdefault(pid, []).append(full)
    picked = []
    for group in by_patch.values():
        if on_default:
            pref = [s for s in group if s in on_default]
            if pref:
                picked.append(pref[0])
                continue
        picked.append(group[0])
    return picked


def _fixer_id(repo: Path, sha: str, advisory: dict) -> int:
    """Stable fixer id: PR, tracker, or CVE."""
    msg = _message(repo, sha)
    m = _PR_ANCHOR.search(msg)
    if m:
        return int(m.group(1) or m.group(2))
    if advisory.get("fix_prs"):
        return int(advisory["fix_prs"][0])
    t = _TRACKER.search(msg)
    if t:
        return int(t.group(1) or t.group(2))
    c = _CVE_NUM.search(advisory.get("cve_id") or "")
    if c:
        return int(c.group(2))
    return int(_rev(repo, sha)[:6], 16)


def _introducer_pr(repo: Path, sha: str) -> int:
    m = _PR_ANCHOR.search(_message(repo, sha))
    return int(m.group(1) or m.group(2)) if m else 0


def mine_fixer(repo: Path, name: str, language: str, fixer: str,
               advisory: dict, max_files: int = 10,
               max_lines: int = 60) -> tuple[SecurityChain | None, str]:
    """Fix commit -> chain, or (None, reason)."""
    spec = get_spec(language)
    base = f"{fixer}^"
    if not _git(repo, "rev-parse", "--verify", base).strip():
        return None, "no_parent"
    changed = _git(repo, "diff", "--name-only", base, fixer).splitlines()
    tests = [f for f in changed if spec.is_test_path(f)]
    srcs = [f for f in changed if spec.is_source(f)]
    if not tests:
        return None, "fix_ships_no_test"
    if not srcs:
        return None, "fix_touches_no_source"

    report_ts = None
    pub = advisory.get("published_at")
    if pub:
        import datetime as _dt
        report_ts = int(_dt.datetime.fromisoformat(pub).replace(
            tzinfo=_dt.timezone.utc).timestamp())

    blamed: Counter[str] = Counter()
    budget = max_lines
    for f in srcs[:max_files]:
        diff = _git(repo, "diff", "--unified=0", base, fixer, "--", f).splitlines()
        oldln = 0
        for d in diff:
            h = re.match(r"@@ -(\d+)(?:,\d+)? ", d)
            if h:
                oldln = int(h.group(1))
                continue
            if d.startswith(("---", "+++")):
                continue
            if d.startswith("-"):
                if budget > 0 and spec.is_blamable(d[1:]):
                    got = changelog_szz._line_introducer(
                        repo, base, f, oldln, report_ts, max_hops=20)
                    if got:
                        blamed[got] += 1
                        budget -= 1
                oldln += 1
        if budget <= 0:
            break
    if not blamed:
        return None, "nothing_to_blame"

    intro, votes = blamed.most_common(1)[0]
    fix_id = _fixer_id(repo, fixer, advisory)
    intro_pr = _introducer_pr(repo, intro)
    chain = SecurityChain(
        instance_id=f"{name}__{intro_pr}-{fix_id}",
        repo=name, language=language,
        introducer_pr=intro_pr, fixer_pr=fix_id,
        pr1_base_sha=_rev(repo, f"{intro}^"), pr1_head_sha=_rev(repo, intro),
        pr2_base_sha=_rev(repo, base), pr2_head_sha=_rev(repo, fixer),
        pr1_date=_date(repo, intro), pr2_date=_date(repo, fixer),
        security={k: advisory.get(k) for k in
                  ("ghsa_id", "cve_id", "cwes", "cvss", "cvss_vector",
                   "severity", "published_at", "advisory_url", "summary")},
        sources=[{"source": "advisory_szz",
                  "evidence": f"{advisory.get('ghsa_id')} -> {fixer[:12]}; "
                              f"SZZ blamed {intro[:12]} ({votes} lines)",
                  "evidence_locus": "advisory_reference",
                  "found_in": "security/fixers.jsonl"}],
        szz={"introducer_sha": intro, "votes": votes,
             "blamed_candidates": len(blamed), "src_files_blamed": len(srcs[:max_files])},
        filters={"pr2_has_test_changes": True, "pr2_n_test_files": len(tests),
                 "pr2_n_src_files": len(srcs)},
    )
    return chain, "ok"
