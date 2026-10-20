"""Django miner: `Fixed #N` commits plus SZZ."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

from . import changelog_szz

FIXED = re.compile(r"Fixed #(\d+)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace",
    ).stdout


def _is_test(f: str) -> bool:
    return f.startswith("tests/") and f.endswith(".py")


def _is_src(f: str) -> bool:
    return f.startswith("django/") and f.endswith(".py")


def _is_blamable(content: str) -> bool:
    s = content.strip()
    return bool(s) and not s.startswith(("#", '"', "'"))


def _szz_introducer(repo: Path, fixer: str, src_files: list[str],
                    max_files: int = 10, max_lines: int = 60,
                    report_ts: int | None = None) -> str | None:
    """Most-blamed commit for the fixed lines."""
    base = fixer + "^"
    blamed: Counter[str] = Counter()
    budget = max_lines
    for f in src_files[:max_files]:
        diff = _git(repo, "diff", "--unified=0", base, fixer, "--", f).splitlines()
        oldln = 0
        for d in diff:
            h = re.match(r"@@ -(\d+)(?:,\d+)? ", d)
            if h:
                oldln = int(h.group(1)); continue
            if d.startswith(("---", "+++")):
                continue
            if d.startswith("-"):
                if budget > 0 and _is_blamable(d[1:]):
                    sha = changelog_szz._line_introducer(
                        repo, base, f, oldln, report_ts, max_hops=20)
                    if sha:
                        blamed[sha] += 1
                        budget -= 1
                oldln += 1
        if budget <= 0:
            break
    if not blamed:
        return None
    return blamed.most_common(1)[0][0]


def mine_chains(repo: Path, ref: str = "HEAD", since: str = "2023-01-01",
                limit: int = 0, ticket_opened_ts=None) -> tuple[list[dict], dict]:
    """(chain records, stats) for `Fixed #N` commits."""
    log = _git(repo, "log", ref, f"--since={since}", "--pretty=%H%x1f%s").splitlines()
    fixers = []
    for line in log:
        if "\x1f" not in line:
            continue
        sha, subj = line.split("\x1f", 1)
        m = FIXED.search(subj)
        if m:
            fixers.append((sha, int(m.group(1))))

    records: list[dict] = []
    n_scan = n_dated = n_no_intro = 0
    for sha, ticket in fixers:
        if limit and n_scan >= limit:
            break
        files = _git(repo, "show", "--name-only", "--pretty=format:", sha).splitlines()
        tests = [f for f in files if _is_test(f)]
        srcs = [f for f in files if _is_src(f)]
        if not (tests and srcs):
            continue
        n_scan += 1
        rts = ticket_opened_ts(ticket) if ticket_opened_ts else None
        if rts is not None:
            n_dated += 1
        intro = _szz_introducer(repo, sha, srcs, report_ts=rts)
        if not intro or intro == sha:
            if rts is not None and not intro:
                # nothing predates the ticket: drop
                n_no_intro += 1
            continue
        if not _git(repo, "rev-parse", "--verify", f"{intro}^").strip():
            continue  # root commit
        im = FIXED.search(_git(repo, "show", "-s", "--format=%s", intro).strip())
        records.append({
            "repo": "django",
            "earlier_pr": int(im.group(1)) if im else 0,
            "later_pr": ticket,
            "category": "introduced",
            "evidence": f"SZZ: ticket #{ticket} fix blames {intro[:10]}",
            "pr1_base_sha": intro + "^",
            "pr1_head_sha": intro,
            "pr2_base_sha": sha + "^",
            "pr2_head_sha": sha,
            "pr1_date": _git(repo, "show", "-s", "--format=%cI", intro).strip(),
            "pr2_date": _git(repo, "show", "-s", "--format=%cI", sha).strip(),
            "flags": [],
        })
    stats = {"fixed_commits": len(fixers), "both_tests_src": n_scan,
             "ticket_dated": n_dated,
             "no_introducer_predates_ticket": n_no_intro,
             "chains": len(records)}
    return records, stats
