"""Run a repo's tests at any commit."""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

WORKTREES_DIR = config.REPO_ROOT / ".worktrees"

# Outcome vocabulary
PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"

TestKey = tuple[str, str]  # (classname, name)


@dataclass
class RunResult:
    sha: str
    label: str
    install_ok: bool = False
    ran: bool = False
    outcomes: dict[TestKey, str] = field(default_factory=dict)
    note: str = ""

    def status_of(self, key: TestKey) -> str | None:
        return self.outcomes.get(key)


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    return (
        path.startswith("testing/")
        or "/tests/" in f"/{path}"
        or "/test/" in f"/{path}"   # singular test/ dir
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
        check=check,
    )


def changed_test_files(repo: Path, base: str, head: str) -> list[str]:
    """Test files changed between base..head that still exist at head."""
    out = _git(repo, "diff", "--name-only", base, head).stdout
    changed = [p for p in out.splitlines() if p and is_test_path(p)]
    present_at_head = set(
        _git(repo, "ls-tree", "-r", "--name-only", head).stdout.splitlines()
    )
    return [p for p in changed if p in present_at_head]


def _parse_junit(xml_path: Path) -> dict[TestKey, str]:
    outcomes: dict[TestKey, str] = {}
    tree = ET.parse(xml_path)
    for case in tree.iter("testcase"):
        key = (case.get("classname", ""), case.get("name", ""))
        status = PASSED
        for child in case:
            tag = child.tag.lower()
            if tag == "failure":
                status = FAILED
            elif tag == "error":
                status = ERROR
            elif tag == "skipped":
                status = SKIPPED
        outcomes[key] = status
    return outcomes


def run_tests(
    repo: Path,
    sha: str,
    test_files: list[str],
    *,
    label: str,
    overlay_from: str | None = None,
    overlay_paths: list[str] | None = None,
    pre_install=None,
    python: str = "3.12",
    install_args: list[str] | None = None,
    clear_addopts: bool = False,
    install_timeout: int = 600,
    test_timeout: int = 600,
    keep: bool = False,
) -> RunResult:
    """Check out sha, install, run test_files."""
    install_args = install_args or ["-e", "."]
    result = RunResult(sha=sha, label=label)
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

        if pre_install is not None:
            pre_install(wt)

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
            ["uv", "pip", "install", "--python", str(py), *install_args],
            cwd=wt, capture_output=True, text=True, timeout=install_timeout,
        )
        if inst.returncode != 0:
            result.note = f"install failed: {inst.stderr.strip()[:300]}"
            return result
        result.install_ok = True

        junit = wt / "_foresight_junit.xml"
        # keep repo addopts unless told to clear
        addopts_override = ["-o", "addopts="] if clear_addopts else []
        # one import error must not abort the run
        proc = subprocess.run(
            [str(py), "-m", "pytest", *test_files,
             f"--junitxml={junit}", "-p", "no:cacheprovider", "-q",
             "--continue-on-collection-errors",
             *addopts_override],
            cwd=wt, capture_output=True, text=True, timeout=test_timeout,
        )
        if not junit.exists():
            result.note = f"no junit; rc={proc.returncode}; {proc.stdout[-300:]}"
            return result
        result.outcomes = _parse_junit(junit)
        result.ran = True
        return result
    except subprocess.TimeoutExpired:
        result.note = "timeout"
        return result
    finally:
        if not keep:
            _git(repo, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)
