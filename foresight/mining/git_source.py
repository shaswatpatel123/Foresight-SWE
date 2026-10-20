"""Rebuild PRs from merge and squash commits."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import Commit, PullRequest

# field and record separators
_FS = "\x1f"
_FORMAT = _FS.join(["%H", "%P", "%aI", "%s", "%b"])

_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from ", re.IGNORECASE)
_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")


def _run_git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        errors="replace",  # non-UTF-8 messages
        check=True,
    )
    return result.stdout


def iter_commits(repo_path: Path, branch: str) -> list[Commit]:
    out = _run_git(repo_path, "log", "-z", f"--format={_FORMAT}", branch)
    commits: list[Commit] = []
    for record in out.split("\0"):
        if not record.strip():
            continue
        parts = record.split(_FS)
        if len(parts) < 5:
            # empty body
            parts = parts + [""] * (5 - len(parts))
        sha, parents, date, subject, body = parts[:5]
        commits.append(
            Commit(
                sha=sha,
                parents=parents.split() if parents else [],
                date=date,
                subject=subject,
                body=body,
            )
        )
    return commits


def _as_pull_request(commit: Commit) -> PullRequest | None:
    m = _MERGE_RE.match(commit.subject)
    if m and len(commit.parents) >= 2:
        return PullRequest(
            number=int(m.group(1)),
            anchor_sha=commit.sha,
            base_sha=commit.parents[0],
            head_sha=commit.sha,
            kind="merge",
            date=commit.date,
            subject=commit.subject,
            message=commit.message,
        )
    m = _SQUASH_RE.search(commit.subject)
    if m and len(commit.parents) == 1:
        return PullRequest(
            number=int(m.group(1)),
            anchor_sha=commit.sha,
            base_sha=commit.parents[0],
            head_sha=commit.sha,
            kind="squash",
            date=commit.date,
            subject=commit.subject,
            message=commit.message,
        )
    return None


def build_pr_index(commits: list[Commit]) -> dict[int, PullRequest]:
    """PR number -> PR; newest wins."""
    index: dict[int, PullRequest] = {}
    for commit in commits:
        pr = _as_pull_request(commit)
        if pr is not None and pr.number not in index:
            index[pr.number] = pr
    return index


def _mainline_shas(commits: list[Commit], by_sha: dict[str, Commit]) -> set[str]:
    """First-parent history of the tip."""
    mainline: set[str] = set()
    cur: str | None = commits[0].sha if commits else None
    while cur and cur in by_sha and cur not in mainline:
        mainline.add(cur)
        parents = by_sha[cur].parents
        cur = parents[0] if parents else None
    return mainline


def attach_merge_branch_text(
    commits: list[Commit],
    pr_index: dict[int, PullRequest],
    max_branch: int = 100,
) -> int:
    """Add branch commit text to merge PRs."""
    by_sha = {c.sha: c for c in commits}
    mainline = _mainline_shas(commits, by_sha)
    enriched = 0
    for pr in pr_index.values():
        if pr.kind != "merge":
            continue
        anchor = by_sha.get(pr.anchor_sha)
        if anchor is None or len(anchor.parents) < 2:
            continue
        texts: list[str] = []
        seen: set[str] = set()
        stack = [anchor.parents[1]]
        overflow = False
        while stack:
            sha = stack.pop()
            if sha in seen or sha in mainline:
                continue
            seen.add(sha)
            if len(seen) > max_branch:
                overflow = True
                break
            commit = by_sha.get(sha)
            if commit is None:
                continue
            texts.append(commit.message)
            stack.extend(commit.parents)
        if overflow or not texts:
            continue
        pr.message = f"{pr.message}\n{chr(10).join(texts)}".strip()
        enriched += 1
    return enriched
