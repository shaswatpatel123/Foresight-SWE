"""Cheap git-only screens: test changes, recency."""
from __future__ import annotations

import datetime as dt
import math
import subprocess

from . import config
from .langspec import get_spec

# keys of the `filters` block
KEYS = ("pr1_has_test_changes", "pr1_n_test_files", "pr1_n_src_files",
        "pr2_has_test_changes", "pr2_n_test_files", "pr2_n_src_files",
        "recency_tier")

REFERENCE = dt.datetime.fromisoformat(config.RECENCY_REFERENCE).replace(
    tzinfo=dt.timezone.utc)


def _instant(value: str | None) -> dt.datetime | None:
    """Parse an ISO date, default UTC."""
    s = (value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def recency_tier(pr1_date: str | None, pr2_date: str | None) -> int | None:
    """Years back to the older PR."""
    a, b = _instant(pr1_date), _instant(pr2_date)
    if not (a and b):
        return None
    years = (REFERENCE - min(a, b)).days / 365.25
    return max(1, math.ceil(years))


def changed_paths(repo_path, spec, base: str, head: str) -> tuple[list[str], list[str]]:
    """Changed (test, source) paths; raises on git error."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", base, head],
        capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            f"git diff {base[:10]}..{head[:10]} in {repo_path} failed "
            f"({proc.returncode}): {proc.stderr.strip().splitlines()[:1]}")
    names = [n for n in proc.stdout.splitlines() if n]
    return ([n for n in names if spec.is_test_path(n)],
            [n for n in names if spec.is_source(n)])


def compute(row: dict) -> dict:
    """The `filters` block for one chain."""
    out = dict.fromkeys(KEYS)
    out["recency_tier"] = recency_tier(row.get("pr1_date"), row.get("pr2_date"))
    try:
        target = config.get_target(row["repo"])
        spec = get_spec(target.language)
        tst, src = changed_paths(target.path, spec,
                                 row["pr2_base_sha"], row["pr2_head_sha"])
        out.update(pr2_has_test_changes=bool(tst),
                   pr2_n_test_files=len(tst), pr2_n_src_files=len(src))
        base, head = row.get("pr1_base_sha"), row.get("pr1_head_sha")
        if base and head:
            tst, src = changed_paths(target.path, spec, base, head)
            out.update(pr1_has_test_changes=bool(tst),
                       pr1_n_test_files=len(tst), pr1_n_src_files=len(src))
    except (Exception, SystemExit):
        # unknown repo raises SystemExit
        pass
    return out
