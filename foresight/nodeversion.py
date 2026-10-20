"""Pick the Node major for a commit."""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

Major = int

# supported Node range
NODE_FLOOR: Major = 12
NODE_CEIL: Major = 24

# (major, Active LTS date)
NODE_LTS: tuple[tuple[Major, str], ...] = (
    (12, "2019-10-22"),
    (14, "2020-10-27"),
    (16, "2021-10-26"),
    (18, "2022-10-25"),
    (20, "2023-10-24"),
    (22, "2024-10-29"),
    (24, "2025-10-28"),
)

# (major, release date): a hard ceiling
NODE_RELEASED: tuple[tuple[Major, str], ...] = (
    (12, "2019-04-23"), (13, "2019-10-22"), (14, "2020-04-21"), (15, "2020-10-20"),
    (16, "2021-04-20"), (17, "2021-10-19"), (18, "2022-04-19"), (19, "2022-10-18"),
    (20, "2023-04-18"), (21, "2023-10-17"), (22, "2024-04-24"), (23, "2024-10-16"),
    (24, "2025-04-22"),
)

ALL_MAJORS = frozenset(range(NODE_FLOOR, NODE_CEIL + 1))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, errors="replace").stdout


def _show(repo: Path, sha: str, path: str) -> str:
    """File at a commit, or ""."""
    p = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"],
                       capture_output=True, text=True, errors="replace")
    return p.stdout if p.returncode == 0 else ""


# ---------------------------------------------------------------- semver -> majors

_CMP = re.compile(r"(>=|<=|>|<|\^|~|=)?\s*v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?")


def majors_from_range(spec: str) -> frozenset[Major]:
    """Allowed majors for a semver range."""
    spec = (spec or "").strip()
    # empty is "no answer", not "any"
    if not spec:
        return frozenset()
    if spec in ("*", "x", "latest"):
        return ALL_MAJORS
    out: set[Major] = set()
    for alt in spec.split("||"):
        alt = alt.strip()
        if not alt:
            continue
        allowed = set(ALL_MAJORS)
        found = False
        for m in _CMP.finditer(alt):
            op, major_s, minor_s, _patch = m.groups()
            if major_s is None:
                continue
            found = True
            major = int(major_s)
            if op in (">=", None, "", "="):
                # "16" is 16; ">=16" is 16 up
                allowed &= (set(range(major, NODE_CEIL + 1)) if op == ">="
                            else {major})
            elif op == ">":
                # ">16.9.0" still allows 16
                allowed &= set(range(major if minor_s else major + 1, NODE_CEIL + 1))
            elif op == "<":
                allowed &= set(range(NODE_FLOOR, major + (1 if minor_s else 0)))
            elif op == "<=":
                allowed &= set(range(NODE_FLOOR, major + 1))
            elif op in ("^", "~"):
                # caret/tilde stay in one major
                allowed &= {major}
        if found:
            out |= allowed
    return frozenset(out & ALL_MAJORS)


# ---------------------------------------------------------------- the four sources

def _from_engines(repo: Path, sha: str) -> frozenset[Major]:
    txt = _show(repo, sha, "package.json")
    if not txt:
        return frozenset()
    try:
        pkg = json.loads(txt)
    except json.JSONDecodeError:
        return frozenset()
    node = ((pkg.get("engines") or {}) if isinstance(pkg.get("engines"), dict) else {}).get("node")
    return majors_from_range(node) if isinstance(node, str) else frozenset()


# .nvmrc LTS codenames
_NVM_CODENAMES = {"fermium": 14, "gallium": 16, "hydrogen": 18,
                  "iron": 20, "jod": 22, "krypton": 24}


def _from_nvmrc(repo: Path, sha: str) -> frozenset[Major]:
    txt = _show(repo, sha, ".nvmrc").strip().lower()
    if not txt:
        return frozenset()
    txt = txt.replace("lts/", "").strip()
    if txt in _NVM_CODENAMES:
        return frozenset({_NVM_CODENAMES[txt]}) & ALL_MAJORS
    m = re.match(r"v?(\d+)", txt)
    return frozenset({int(m.group(1))}) & ALL_MAJORS if m else frozenset()


_CI_NODE = re.compile(r"node[-_]version\s*:\s*(\[[^\]]*\]|['\"]?[\w.*]+['\"]?)")


def _from_ci(repo: Path, sha: str) -> frozenset[Major]:
    """Majors named in CI `node-version:`."""
    names = [f for f in _git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
             if re.match(r"\.github/workflows/.*\.ya?ml$", f)]
    out: set[Major] = set()
    for f in names[:12]:                      # cap files read
        for raw in _CI_NODE.findall(_show(repo, sha, f)):
            if "matrix" in raw or "env." in raw:
                continue
            for num in re.findall(r"(\d+)", raw):
                v = int(num)
                if NODE_FLOOR <= v <= NODE_CEIL:
                    out.add(v)
    return frozenset(out)


def _commit_date(repo: Path, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%cs", sha).strip()


def _from_lts_date(repo: Path, sha: str) -> frozenset[Major]:
    """Newest LTS at commit time (a guess)."""
    d = _commit_date(repo, sha)
    if not d:
        return frozenset()
    best = None
    for major, since in NODE_LTS:
        if since <= d:
            best = major
    return frozenset({best}) if best else frozenset()


_SOURCES = (("engines", _from_engines), ("nvmrc", _from_nvmrc),
            ("ci", _from_ci), ("lts_by_date", _from_lts_date))


def existed_by(date: str) -> frozenset[Major]:
    """Majors released by `date`."""
    if not date:
        return ALL_MAJORS
    return frozenset(m for m, rel in NODE_RELEASED if rel <= date) & ALL_MAJORS


def allowed_majors(repo: Path, sha: str) -> tuple[frozenset[Major], set[str]]:
    """First source that answers wins."""
    for name, fn in _SOURCES:
        got = fn(repo, sha)
        if got:
            capped = got & existed_by(_commit_date(repo, sha))
            # clamp emptied it: keep the raw answer
            return (capped or got), {name}
    return frozenset(), set()


def era_majors(repo: Path, base_sha: str, head_sha: str
               ) -> tuple[frozenset[Major], set[str], str]:
    """Majors for one era (base and head)."""
    a, sa = allowed_majors(repo, base_sha)
    b, sb = allowed_majors(repo, head_sha)
    src = sa | sb
    if not a and not b:
        return frozenset(), src, "undeclared"
    if not a or not b:
        return (a or b), src, "ok"
    both = a & b
    if not both:
        return b, src, "empty"
    return both, src, "ok"


# ---------------------------------------------------------------- the cover

def cover(sets: list[frozenset[Major]]) -> list[Major]:
    """Fewest majors covering all sets; prefer even, newer."""
    remaining = [s for s in sets if s]
    picks: list[Major] = []
    while remaining:
        counts: dict[Major, int] = {}
        for s in remaining:
            for v in s:
                counts[v] = counts.get(v, 0) + 1
        best = max(counts, key=lambda v: (counts[v], v % 2 == 0, v))
        picks.append(best)
        remaining = [s for s in remaining if best not in s]
    return sorted(picks)


# ---------------------------------------------------------------- package manager

def package_manager(repo: Path, sha: str) -> tuple[str, str | None, str]:
    """(name, version, source) of the package manager."""
    txt = _show(repo, sha, "package.json")
    if txt:
        try:
            pm = json.loads(txt).get("packageManager")
        except json.JSONDecodeError:
            pm = None
        if isinstance(pm, str) and pm:
            name, _, ver = pm.partition("@")
            return name.strip(), (ver.split("+")[0].strip() or None), "packageManager"
    names = set(_git(repo, "ls-tree", "--name-only", sha).split())
    for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                       ("package-lock.json", "npm")):
        if lock in names:
            return name, None, "lockfile"
    return "npm", None, "default"


def fmt(v: Major) -> str:
    """Image tag form, e.g. "20"."""
    return str(v)


def _selfcheck(repos: list[str]) -> None:
    import argparse  # noqa: F401
    from foresight import config
    print(f"{'repo':10s} {'date':10s} {'majors':26s} {'source':12s} {'pm'}")
    for name in repos:
        try:
            t = config.get_target(name)
        except SystemExit:
            print(f"{name:10s} not in TARGETS"); continue
        if not t.path.exists():
            print(f"{name:10s} clone missing"); continue
        for when in ("5 years ago", "3 years ago", "now"):
            sha = _git(t.path, "rev-list", "-1", f"--before={when}",
                       t.main_branch).strip()
            if not sha:
                continue
            got, src = allowed_majors(t.path, sha)
            pm, pmv, pms = package_manager(t.path, sha)
            shown = ",".join(str(x) for x in sorted(got)) or "-"
            print(f"{name:10s} {_commit_date(t.path, sha):10s} {shown:26s} "
                  f"{(','.join(src) or 'NONE'):12s} {pm}@{pmv or '?'} ({pms})")


if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default="svelte,vite,vuecore,axios")
    a = ap.parse_args()
    _selfcheck([r.strip() for r in a.repos.split(",") if r.strip()])
