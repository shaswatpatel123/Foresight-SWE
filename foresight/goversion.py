"""Pick the Go version for a commit."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .pyversion import pierce  # shared interval cover

Version = tuple[int, int]

# committer date of the golang/go release tag
GO_RELEASED: tuple[tuple[Version, str], ...] = (
    ((1, 11), "2018-08-24"), ((1, 12), "2019-02-25"), ((1, 13), "2019-09-03"),
    ((1, 14), "2020-02-25"), ((1, 15), "2020-08-11"), ((1, 16), "2021-02-16"),
    ((1, 17), "2021-08-16"), ((1, 18), "2022-03-15"), ((1, 19), "2022-08-02"),
    ((1, 20), "2023-02-01"), ((1, 21), "2023-08-08"), ((1, 22), "2024-02-06"),
    ((1, 23), "2024-08-13"), ((1, 24), "2025-02-11"), ((1, 25), "2025-08-12"),
    ((1, 26), "2026-02-10"), ((1, 27), "2026-08-19"),
)
GO_FLOOR: Version = (1, 11)          # first with go.mod
GO_CEIL: Version = GO_RELEASED[-1][0]

_GO_DIRECTIVE = re.compile(r"^go\s+(\d+)\.(\d+)(?:\.\d+)?\s*$", re.M)
_TOOLCHAIN = re.compile(r"^toolchain\s+go(\d+)\.(\d+)(?:\.\d+)?\s*$", re.M)
_CI_GO = re.compile(r"go-version:\s*\[?\s*['\"]?(\d+)\.(\d+)", re.I)
# go.mod may sit below the root
_GOMOD_CANDIDATES = ("go.mod", "server/go.mod", "src/go.mod", "backend/go.mod")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, errors="replace").stdout


def _show(repo: Path, sha: str, path: str) -> str:
    return _git(repo, "show", f"{sha}:{path}")


def _commit_date(repo: Path, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%cI", sha).strip()[:10]


def existed_by(date: str) -> Version:
    """Newest Go released by `date`."""
    ok = [v for v, rel in GO_RELEASED if rel <= date]
    return max(ok) if ok else GO_FLOOR


def _gomod(repo: Path, sha: str) -> str:
    for cand in _GOMOD_CANDIDATES:
        text = _show(repo, sha, cand)
        if text.strip():
            return text
    return ""


def _from_ci(repo: Path, sha: str) -> Version | None:
    names = [l for l in _git(repo, "ls-tree", "--name-only", "-r", sha,
                             ".github/workflows/").splitlines()
             if l.endswith((".yml", ".yaml"))]
    found: list[Version] = []
    for n in names[:20]:
        for maj, minor in _CI_GO.findall(_show(repo, sha, n)):
            found.append((int(maj), int(minor)))
    return min(found) if found else None


def bounds(repo: Path, sha: str) -> tuple[Version, Version, set[str]]:
    """(lo, hi, sources) for one commit."""
    date = _commit_date(repo, sha)
    ceiling = existed_by(date) if date else GO_CEIL
    mod = _gomod(repo, sha)

    m = _TOOLCHAIN.search(mod)
    if m:
        pin = (int(m.group(1)), int(m.group(2)))
        pin = min(max(pin, GO_FLOOR), ceiling)
        return pin, pin, {"toolchain"}

    m = _GO_DIRECTIVE.search(mod)
    if m:
        lo = max((int(m.group(1)), int(m.group(2))), GO_FLOOR)
        return min(lo, ceiling), ceiling, {"godirective"}

    ci = _from_ci(repo, sha)
    if ci:
        lo = max(ci, GO_FLOOR)
        return min(lo, ceiling), ceiling, {"ci"}

    return ceiling, ceiling, {"latest_by_date"}


def era_interval(repo: Path, base_sha: str, head_sha: str):
    """Go interval for one era (base and head)."""
    lo1, hi1, s1 = bounds(repo, base_sha)
    lo2, hi2, s2 = bounds(repo, head_sha)
    src = s1 | s2
    lo, hi = max(lo1, lo2), min(hi1, hi2)
    if lo > hi:
        # ends disagree: use head's range
        return lo2, hi2, src, "empty"
    status = "undeclared" if src == {"latest_by_date"} else "ok"
    return lo, hi, src, status


def cover(intervals: list[tuple[Version, Version]]) -> list[Version]:
    return pierce(intervals)


def fmt(v: Version) -> str:
    return f"{v[0]}.{v[1]}"
