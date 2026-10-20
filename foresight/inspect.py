"""Per-chain review report."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config
from .execution.runner import is_test_path

SLUGS = {name: t.slug for name, t in config.TARGETS.items()}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, errors="replace").stdout


def _changed_files(repo: Path, base: str, head: str) -> tuple[list[str], list[str]]:
    files = [f for f in _git(repo, "diff", "--name-only", base, head).splitlines() if f]
    tests = [f for f in files if is_test_path(f)]
    src = [f for f in files if not is_test_path(f)]
    return src, tests


def _diff(repo: Path, base: str, head: str, paths: list[str], max_lines: int) -> str:
    if not paths:
        return "(none)"
    out = _git(repo, "diff", base, head, "--", *paths)
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (+{len(lines) - max_lines} more lines)"]
    return "\n".join(lines)


def chain_report(chain: dict, max_diff_lines: int = 120) -> str:
    repo = chain["repo"]
    slug = SLUGS.get(repo, repo)
    path = config.REPOS_DIR / repo
    pr1, pr2 = chain["earlier_pr"], chain["later_pr"]
    src, tests = _changed_files(path, chain["pr2_base_sha"], chain["pr2_head_sha"])
    ant = [n for _, n in chain.get("anticipation_tests", [])]
    pr1_own = [n for _, n in chain.get("pr1_f2p_tests", [])]

    parts = [
        f"## {repo}: PR{pr1} -> PR{pr2}  [{chain['category']}]",
        f"- evidence: `{chain['evidence']}`",
        f"- PR1 (intent):  https://github.com/{slug}/pull/{pr1}",
        f"- PR2 (the fix): https://github.com/{slug}/pull/{pr2}",
        f"- anticipation tests ({len(ant)}): {ant}",
        f"- PR1 own fail->pass tests ({len(pr1_own)}): {pr1_own}",
        "",
        "### PR2 fix (source changes)",
        "```diff",
        _diff(path, chain["pr2_base_sha"], chain["pr2_head_sha"], src, max_diff_lines),
        "```",
        "### PR2 test changes (the anticipation behavior)",
        "```diff",
        _diff(path, chain["pr2_base_sha"], chain["pr2_head_sha"], tests, max_diff_lines),
        "```",
        "",
    ]
    return "\n".join(parts)


def build_report(chains: list[dict]) -> str:
    chains = sorted(chains, key=lambda c: (c["repo"], c["later_pr"]))
    header = [f"# Dataset review — {len(chains)} validated chains\n"]
    return "\n".join(header + [chain_report(c) for c in chains])
