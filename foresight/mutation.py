"""Mutation check: do the tests catch bad fixes?"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config
from .execution.runner import ERROR, FAILED, is_test_path, run_tests

# single-token swaps, in order
_SWAPS: list[tuple[str, str]] = [
    (" == ", " != "), (" != ", " == "),
    (" <= ", " > "), (" >= ", " < "),
    (" < ", " >= "), (" > ", " <= "),
    (" and ", " or "), (" or ", " and "),
    (" is not ", " is "), (" is ", " is not "),
    ("True", "False"), ("False", "True"),
]
_INT = re.compile(r"(?<![\w.])(\d+)(?![\w.])")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, errors="replace").stdout


_NONCODE_PREFIX = ("#", '"', "'", "*", ":", ">>>", "..", "-", "|")


def _is_code_line(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith(_NONCODE_PREFIX):
        return False
    # logic lines only, not prose
    return any(tok in s for tok in ("=", "(", " if ", " or ", " and ", " is ",
                                    "return", "<", ">", "==", "!="))


def _added_source_lines(repo: Path, base: str, head: str) -> dict[str, list[str]]:
    """Source file -> added code lines."""
    files = [f for f in _git(repo, "diff", "--name-only", base, head).splitlines()
             if f and f.endswith(".py") and not is_test_path(f)]
    out: dict[str, list[str]] = {}
    for f in files:
        diff = _git(repo, "diff", base, head, "--", f).splitlines()
        added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
        added = [l for l in added if _is_code_line(l)]
        if added:
            out[f] = added
    return out


def _mutate_line(line: str) -> str | None:
    for a, b in _SWAPS:
        if a in line:
            return line.replace(a, b, 1)
    m = _INT.search(line)
    if m:
        n = int(m.group(1))
        return line[:m.start()] + str(n + 1) + line[m.end():]
    return None


def generate_mutants(repo: Path, base: str, head: str, max_mutants: int):
    """List of (file, original, mutant, description)."""
    mutants = []
    for f, added in _added_source_lines(repo, base, head).items():
        content = _git(repo, "show", f"{head}:{f}")
        for line in added:
            if len(mutants) >= max_mutants:
                return mutants
            mutated = _mutate_line(line)
            if mutated is None or mutated == line or content.count(line) != 1:
                continue
            mutants.append((f, content, content.replace(line, mutated, 1),
                            f"{f}: {line.strip()[:60]!r} -> {mutated.strip()[:60]!r}"))
    return mutants


def check_chain(chain: dict, *, python: str, install_args: list[str],
                max_mutants: int = 4) -> dict:
    repo = config.REPOS_DIR / chain["repo"]
    head = chain["pr2_head_sha"]
    test_files = chain["test_files"]
    targets = {tuple(k) for k in chain["anticipation_tests"]}
    mutants = generate_mutants(repo, chain["pr2_base_sha"], head, max_mutants)
    result = {"repo": chain["repo"], "later_pr": chain["later_pr"],
              "n_mutants": len(mutants), "killed": 0, "survived": 0, "detail": []}
    for f, _orig, mutant_content, desc in mutants:
        def _write(wt: Path, _f=f, _c=mutant_content):
            (wt / _f).write_text(_c)
        r = run_tests(repo, head, test_files, label="mut",
                      pre_install=_write, python=python, install_args=install_args)
        if not r.ran:
            verdict = "no_run"
        elif any(r.outcomes.get(t) in (FAILED, ERROR) for t in targets):
            verdict = "killed"
            result["killed"] += 1
        else:
            verdict = "survived"
            result["survived"] += 1
        result["detail"].append({"mutation": desc, "verdict": verdict})
    scored = result["killed"] + result["survived"]
    result["mutation_score"] = round(result["killed"] / scored, 2) if scored else None
    return result
