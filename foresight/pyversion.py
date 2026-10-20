"""Pick the Python version for a commit."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

Version = tuple[int, int]

# lowest uv-managed Python
UV_FLOOR: Version = (3, 8)
SANE_CEIL: Version = (3, 20)

_CLASSIF_MINOR = re.compile(r"Programming Language :: Python :: (\d+)\.(\d+)")
# major-only classifier: no version
_CLASSIF_ANY = re.compile(r"Programming Language :: Python\b")
_TOX_TOKEN = re.compile(r"\bpy(\d)(\d+)\b")
# tox braces: `py3{13,12}`, `py{37,38}`
_TOX_BRACE = re.compile(r"\bpy(\d*)\{([\d,\s]+)\}")
_REQUIRES = re.compile(
    r"""(?:requires-python|python_requires)\s*[=:]\s*["']([^"']+)["']""")
# Poetry `python = ">=3.7"`
_POETRY_PY = re.compile(
    r"\[tool\.poetry\.dependencies\][^\[]*?^\s*python\s*=\s*[\"']([^\"']+)[\"']",
    re.M | re.S)
_GE = re.compile(r">=\s*(\d+)\.(\d+)")
_LT = re.compile(r"<\s*(\d+)\.(\d+)")
# CI: inline list, block list, scalar
_CI_INLINE = re.compile(r"python-version:\s*\[([^\]]*)\]")
_CI_BLOCK = re.compile(
    r"python-version:\s*(?:#[^\n]*)?\n((?:\s*-\s*[\"']?\d+\.\d+[\"']?\s*\n)+)")
_CI_SCALAR = re.compile(r"python-version:\s*[\"']?(\d+)\.(\d+)[\"']?\s*$", re.M)
_VER = re.compile(r"(\d+)\.(\d+)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True,
                          errors="replace").stdout


def _show(repo: Path, sha: str, path: str) -> str:
    return _git(repo, "show", f"{sha}:{path}")


def _sane(vs) -> list[Version]:
    return [(a, b) for a, b in vs if a == 3 and 0 <= b <= SANE_CEIL[1]]


def _tox_versions(text: str) -> list[Version]:
    """Versions from tox env names."""
    out = [(int(a), int(b)) for a, b in _TOX_TOKEN.findall(text)]
    for prefix, inner in _TOX_BRACE.findall(text):
        for part in (p.strip() for p in inner.split(",")):
            if not part.isdigit():
                continue
            tok = prefix + part            # "3" + "13" -> "313"
            if len(tok) >= 2:
                out.append((int(tok[0]), int(tok[1:])))
    return _sane(out)


def _from_ci_text(text: str) -> list[Version]:
    """Versions from one CI file."""
    out: list[Version] = []
    for m in _CI_INLINE.finditer(text):
        out += [(int(a), int(b)) for a, b in _VER.findall(m.group(1))]
    for m in _CI_BLOCK.finditer(text):
        out += [(int(a), int(b)) for a, b in _VER.findall(m.group(1))]
    out += [(int(a), int(b)) for a, b in _CI_SCALAR.findall(text)]
    out += _tox_versions(text)
    return _sane(out)


def bounds(repo: Path, sha: str) -> tuple[Version | None, Version | None, set[str]]:
    """(lo, hi, sources) for one commit."""
    seen: list[Version] = []
    lo: Version | None = None
    hi_cap: Version | None = None
    sources: set[str] = set()

    for f in ("setup.py", "setup.cfg", "pyproject.toml"):
        t = _show(repo, sha, f)
        if not t:
            continue
        found = _sane([(int(a), int(b)) for a, b in _CLASSIF_MINOR.findall(t)])
        if found:
            seen += found
            sources.add("classifiers")
        elif _CLASSIF_ANY.search(t):
            sources.add("classifiers_major_only")   # no version
        spec = None
        if (m := _REQUIRES.search(t)):
            spec, src = m.group(1), "requires_python"
        elif (m := _POETRY_PY.search(t)):
            spec, src = m.group(1), "poetry"
        if spec:
            sources.add(src)
            if (g := _GE.search(spec)):
                v = (int(g.group(1)), int(g.group(2)))
                lo = max(lo, v) if lo else v
            if (u := _LT.search(spec)):          # rare upper bound
                hi_cap = (int(u.group(1)), int(u.group(2)) - 1)

    t = _show(repo, sha, "tox.ini")
    if t and (v := _tox_versions(t)):
        seen += v
        sources.add("tox")
    t = _show(repo, sha, ".travis.yml")
    if t and (v := _from_ci_text(t)):
        seen += v
        sources.add("travis")

    tree = _git(repo, "ls-tree", "--name-only", f"{sha}:.github/workflows")
    for wf in [w for w in tree.splitlines() if w.endswith((".yml", ".yaml"))][:12]:
        if (v := _from_ci_text(_show(repo, sha, f".github/workflows/{wf}"))):
            seen += v
            sources.add("ci")

    hi = max(seen) if seen else None
    if hi and hi_cap:
        hi = min(hi, hi_cap)
    if lo is None and seen:
        lo = min(seen)
    return lo, hi, sources


def era_interval(repo: Path, base_sha: str, head_sha: str,
                 floor: Version = UV_FLOOR):
    """Python interval for one era (base and head)."""
    lo1, hi1, s1 = bounds(repo, base_sha)
    lo2, hi2, s2 = bounds(repo, head_sha)
    src = s1 | s2
    his = [h for h in (hi1, hi2) if h]
    if not his:
        return None, None, src, "undeclared"
    lo = max([lo1 or floor, lo2 or floor, floor])
    hi = min(his)
    if lo > hi:
        # not fatal: try `hi` anyway
        return lo, hi, src, "empty"
    return lo, hi, src, "ok"


def pierce(intervals: list[tuple[Version, Version]]) -> list[Version]:
    """Fewest versions covering all intervals."""
    pins: list[Version] = []
    for lo, hi in sorted(intervals, key=lambda x: (x[1], x[0])):
        if not pins or pins[-1] < lo:
            pins.append(hi)
    return pins
