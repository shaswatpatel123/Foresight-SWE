"""Run Django's runtests.py at any commit."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .. import config
from .runner import PASSED, FAILED, ERROR, SKIPPED, RunResult, TestKey, _git

WORKTREES_DIR = config.REPO_ROOT / ".worktrees"

# `name (dotted.id) ... result`, not line-anchored
_LINE = re.compile(
    r"\(([\w.]+)\)\s+\.\.\.\s+"
    r"(ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b",
)


def file_to_label(path: str) -> str | None:
    """`tests/dates/tests.py` -> `dates.tests`; non-tests/ paths -> None."""
    if not path.endswith(".py"):
        return None
    if not path.startswith("tests/"):
        return None
    rel = path[len("tests/"):]
    if rel.endswith("/__init__.py"):
        rel = rel[: -len("/__init__.py")]
    elif rel.endswith(".py"):
        rel = rel[:-3]
    return rel.replace("/", ".")


def is_django_test_path(path: str) -> bool:
    return file_to_label(path) is not None


def changed_django_test_files(repo: Path, base: str, head: str) -> list[str]:
    """tests/ files changed between base..head that still exist at head."""
    out = _git(repo, "diff", "--name-only", base, head).stdout
    changed = [p for p in out.splitlines() if p and is_django_test_path(p)]
    present = set(_git(repo, "ls-tree", "-r", "--name-only", head).stdout.splitlines())
    return [p for p in changed if p in present]


def _parse_runtests(stdout: str) -> dict[TestKey, str]:
    """Parse runtests output into test outcomes."""
    status_map = {
        "ok": PASSED,
        "FAIL": FAILED,
        "ERROR": ERROR,
        "skipped": SKIPPED,
        "expected failure": SKIPPED,   # xfail: no signal
        "unexpected success": FAILED,  # xpass: a failure
    }
    outcomes: dict[TestKey, str] = {}
    for m in _LINE.finditer(stdout):
        dotted, word = m.group(1), m.group(2)
        if "." not in dotted:
            continue
        classname, _, name = dotted.rpartition(".")
        outcomes[(classname, name)] = status_map[word]
    return outcomes


def run_django_tests(
    repo: Path,
    sha: str,
    test_files: list[str],
    *,
    label: str,
    overlay_from: str | None = None,
    overlay_paths: list[str] | None = None,
    python: str = "3.12",
    settings: str = "test_sqlite",
    install_timeout: int = 900,
    test_timeout: int = 1200,
    keep: bool = False,
) -> RunResult:
    """run_tests, but for Django."""
    result = RunResult(sha=sha, label=label)
    labels = sorted({lbl for p in test_files if (lbl := file_to_label(p))})
    if not labels:
        result.note = "no django test labels"
        return result

    WORKTREES_DIR.mkdir(exist_ok=True)
    wt = WORKTREES_DIR / f"{label}-{sha[:10]}"
    if wt.exists():
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)

    try:
        add = _git(repo, "worktree", "add", "-f", "--detach", str(wt), sha, check=False)
        if add.returncode != 0:
            result.note = f"worktree add failed: {add.stderr.strip()[:200]}"
            return result

        if overlay_from:
            paths = overlay_paths or test_files
            co = _git(wt, "checkout", overlay_from, "--", *paths, check=False)
            if co.returncode != 0:
                result.note = f"overlay checkout failed: {co.stderr.strip()[:200]}"
                return result

        venv = wt / ".venv"
        py = venv / "bin" / "python"
        mk = subprocess.run(
            ["uv", "venv", "--python", python, str(venv)],
            capture_output=True, text=True,
        )
        if mk.returncode != 0:
            result.note = f"venv failed: {mk.stderr.strip()[:200]}"
            return result

        inst = subprocess.run(
            ["uv", "pip", "install", "--python", str(py), "-e", "."],
            cwd=wt, capture_output=True, text=True, timeout=install_timeout,
        )
        if inst.returncode != 0:
            result.note = f"install failed: {inst.stderr.strip()[:300]}"
            return result
        result.install_ok = True

        proc = subprocess.run(
            [str(py), "tests/runtests.py", f"--settings={settings}",
             "--parallel", "1", "--verbosity", "2", *labels],
            cwd=wt, capture_output=True, text=True, timeout=test_timeout,
        )
        combined = proc.stdout + "\n" + proc.stderr
        result.outcomes = _parse_runtests(combined)
        if not result.outcomes:
            result.note = f"no tests parsed; rc={proc.returncode}; {combined[-400:]}"
            return result
        result.ran = True
        return result
    except subprocess.TimeoutExpired:
        result.note = "timeout"
        return result
    finally:
        if not keep:
            _git(repo, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)
