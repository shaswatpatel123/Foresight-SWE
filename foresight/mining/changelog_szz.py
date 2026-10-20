"""Find fixers in changelogs, introducers via SZZ blame."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .models import ChainEdge, PullRequest

# drop chains with a longer PR1->PR2 gap
MAX_GAP_DAYS = 730  # 2 years


def _gap_days(date1: str, date2: str) -> int | None:
    def parse(s):
        try:
            return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    a, b = parse(date1), parse(date2)
    return abs((b - a).days) if a and b else None

_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace",
    ).stdout


def find_bugfix_fragments(
    repo: Path, changelog_dir: str, categories: tuple[str, ...]
) -> dict[str, str]:
    """Fragment number -> commit that added it."""
    cat_alt = "|".join(re.escape(c) for c in categories)
    frag_re = re.compile(rf"{re.escape(changelog_dir)}/(\d+)\.(?:{cat_alt})(?:\.\w+)?$")
    out = _git(repo, "log", "--diff-filter=A", "--format=%H", "--name-only",
               "--", f"{changelog_dir}/")
    frag_commit: dict[str, str] = {}
    cur: str | None = None
    for line in out.splitlines():
        s = line.strip()
        if re.fullmatch(r"[0-9a-f]{40}", s):
            cur = s
        elif cur:
            m = frag_re.search(s)
            if m:
                frag_commit.setdefault(m.group(1), cur)
    return frag_commit


_PULL_RE = re.compile(r"(?::(?:pull|pr):`|#)(\d+)")
# changelog heading styles
_RST_UNDERLINE_RE = re.compile(r'^[=~\-^"\'+*`]{3,}\s*$')
_MD_HEADING_RE = re.compile(r'^#{1,6}\s+(.+?)\s*#*$')          # `## Bug fixes`
_LABEL_RE = re.compile(r'^([A-Za-z][\w /&\-]{0,40}):\s*$')      # `Fixes:`
_BUG_HEADING_RE = re.compile(r'bug|fix', re.I)
# a fix bullet: `- Fix ...` or `- [FIXED] ...`
_FIX_LINE_RE = re.compile(r'^\s*[-*]\s*(?:\[(?:fix|fixed|bug)[^\]]*\]|fix(?:e[sd])?\b|bugfix)', re.I)


def find_bugfix_prs_changelog(repo: Path, changelog_file: str) -> set[int]:
    """PR numbers under bug-fix changelog sections."""
    text = _git(repo, "show", f"HEAD:{changelog_file}")
    lines = text.splitlines()
    prs: set[int] = set()
    in_bug = False
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        heading = None
        if line.strip() and _RST_UNDERLINE_RE.match(nxt):
            heading = line.strip()
        elif (m := _MD_HEADING_RE.match(line)):
            heading = m.group(1).strip()
        elif (m := _LABEL_RE.match(line)):
            heading = m.group(1).strip()
        if heading is not None:
            in_bug = bool(_BUG_HEADING_RE.search(heading))
            continue
        # in a bug section, or a fix bullet
        if in_bug or _FIX_LINE_RE.match(line):
            for m in _PULL_RE.finditer(line):
                prs.add(int(m.group(1)))
    return prs


# --- matplotlib github_stats: FIX/BUG-titled PRs
_GHPULL_RE = re.compile(r":ghpull:`(\d+)`:\s*(.*)")
_MPL_FIX_RE = re.compile(r"^(?:FIX|Fix|BUG|Bug)\b")
_GHSTATS_VER_RE = re.compile(r"github_stats_(\d+)\.(\d+)")


def find_bugfix_prs_ghstats(repo: Path, glob_pat: str,
                            min_version: tuple[int, int] = (3, 8)) -> set[int]:
    """Fixer PRs from recent github_stats files."""
    prs: set[int] = set()
    for p in sorted(repo.glob(glob_pat)):
        m = _GHSTATS_VER_RE.search(p.name)
        if m and (int(m.group(1)), int(m.group(2))) < min_version:
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            gm = _GHPULL_RE.search(line)
            if not gm:
                continue
            num, title = int(gm.group(1)), gm.group(2).strip()
            if title.lower().startswith("backport"):
                continue
            if _MPL_FIX_RE.match(title):
                prs.add(num)
    return prs


def find_bugfix_commits(repo: Path, pattern: str, ref: str = "HEAD",
                        since: str = "") -> list[str]:
    """Fixer commits whose subject matches `pattern`."""
    rx = re.compile(pattern)
    out: list[str] = []
    args = [ref] + ([f"--since={since}"] if since else [])
    log = _git(repo, "log", *args, "--pretty=%H%x1f%s")
    for line in log.splitlines():
        if "\x1f" not in line:
            continue
        sha, subj = line.split("\x1f", 1)
        if rx.search(subj):
            out.append(sha)
    return out


def _pr_of_commit(repo: Path, sha: str, anchor2pr: dict[str, int]) -> int | None:
    """Commit -> PR number, if known."""
    if sha in anchor2pr:
        return anchor2pr[sha]
    subj = _git(repo, "show", "-s", "--format=%s", sha).strip()
    m = _SQUASH_RE.search(subj)
    return int(m.group(1)) if m else None


def _is_blamable(content: str) -> bool:
    """Not blank, not a comment."""
    s = content.strip()
    if not s:
        return False
    return not s.startswith(("#", '"', "'"))


def _commit_ts(repo: Path, sha: str) -> int | None:
    out = _git(repo, "show", "-s", "--format=%ct", sha).strip()
    return int(out) if out.isdigit() else None


def _line_introducer(repo: Path, base: str, path: str, line: int,
                     report_ts: int | None, max_hops: int) -> str | None:
    """Newest commit on this line before the report."""
    bl = _git(repo, "blame", "-l", "-L", f"{line},{line}", base, "--", path).split()
    if not bl:
        return None
    sha = bl[0].lstrip("^")
    if report_ts is None:
        return sha
    ts = _commit_ts(repo, sha)
    if ts is not None and ts <= report_ts:
        return sha
    out = _git(repo, "log", "-L", f"{line},{line}:{path}", "--format=%H %ct", "-s", base)
    hops = 0
    for m in re.finditer(r"^([0-9a-f]{40}) (\d+)$", out, re.M):
        hops += 1
        if hops > max_hops:
            break
        if int(m.group(2)) <= report_ts:
            return m.group(1)
    # every commit is after the report
    return None


# porcelain header: "<sha> <orig> <final> [<n>]"
_PORCELAIN_HDR = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: \d+)?$")

# max lines per `git blame` call
_BLAME_CHUNK = 120


def _blame_lines(repo: Path, base: str, path: str,
                 lines: list[int]) -> dict[int, str]:
    """Blame many lines in one call."""
    if not lines:
        return {}
    args = ["blame", "--porcelain"]
    for ln in lines:
        args += ["-L", f"{ln},{ln}"]
    proc = subprocess.run(
        ["git", "-C", str(repo), *args, base, "--", path],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        return _blame_lines_individually(repo, base, path, lines)
    out: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        m = _PORCELAIN_HDR.match(line)
        if m:
            out[int(m.group(3))] = m.group(1)
    return out


def _blame_lines_individually(repo: Path, base: str, path: str,
                              lines: list[int]) -> dict[int, str]:
    """Fallback: one blame per line."""
    out: dict[int, str] = {}
    for ln in lines:
        for line in _git(repo, "blame", "--porcelain", "-L", f"{ln},{ln}",
                         base, "--", path).splitlines():
            m = _PORCELAIN_HDR.match(line)
            if m:
                out[int(m.group(3))] = m.group(1)
                break
    return out


def _accept_blamed(repo: Path, base: str, path: str, line: int, sha: str,
                   report_ts: int | None, max_hops: int,
                   ts_cache: dict[str, int | None]) -> str | None:
    """Reject blamed commits after the report."""
    if report_ts is None:
        return sha
    if sha not in ts_cache:
        ts_cache[sha] = _commit_ts(repo, sha)
    ts = ts_cache[sha]
    if ts is not None and ts <= report_ts:
        return sha
    out = _git(repo, "log", "-L", f"{line},{line}:{path}", "--format=%H %ct", "-s", base)
    hops = 0
    for m in re.finditer(r"^([0-9a-f]{40}) (\d+)$", out, re.M):
        hops += 1
        if hops > max_hops:
            break
        if int(m.group(2)) <= report_ts:
            return m.group(1)
    return None


def szz_introducer(
    repo: Path,
    pr2: PullRequest,
    resolve,
    is_source,
    is_blamable,
    *,
    max_files: int = 10,
    max_blame_lines: int = 60,
    report_ts: int | None = None,
    max_hops: int = 20,
) -> int | None:
    """PR of the most-blamed introducer, or None."""
    base, head = pr2.base_sha, pr2.head_sha
    files = [
        f for f in _git(repo, "diff", "--name-only", base, head).splitlines()
        if is_source(f)
    ]
    blamed: Counter[str] = Counter()
    budget = max_blame_lines
    ts_cache: dict[str, int | None] = {}
    for f in files[:max_files]:
        diff = _git(repo, "diff", "--unified=0", base, head, "--", f).splitlines()
        oldln = 0
        targets: list[int] = []
        for d in diff:
            h = re.match(r"@@ -(\d+)(?:,\d+)? ", d)
            if h:
                oldln = int(h.group(1))
                continue
            if d.startswith(("---", "+++")):
                continue
            if d.startswith("-"):
                # line 0 is not blamable
                if is_blamable(d[1:]) and oldln >= 1:
                    targets.append(oldln)
                oldln += 1
            # added lines keep oldln
        # blame in bulk, count in order
        for i in range(0, len(targets), _BLAME_CHUNK):
            if budget <= 0:
                break
            chunk = targets[i:i + _BLAME_CHUNK]
            first_pass = _blame_lines(repo, base, f, chunk)
            for ln in chunk:
                if budget <= 0:
                    break
                sha = first_pass.get(ln)
                if sha is not None:
                    sha = _accept_blamed(repo, base, f, ln, sha,
                                         report_ts, max_hops, ts_cache)
                if sha:
                    blamed[sha] += 1
                    budget -= 1
        if budget <= 0:
            break
    # most-blamed first
    for top_sha, _ in blamed.most_common():
        pr = resolve(top_sha)
        if pr is not None:
            return pr
    return None


def find_chain_edges(
    repo: Path,
    pr_index: dict[int, PullRequest],
    spec=None,
    *,
    changelog_dir: str = "",
    changelog_file: str = "",
    changelog_glob: str = "",
    bugfix_commit_re: str = "",
    bugfix_commit_since: str = "",
    fixer_prs: set[int] | None = None,
    categories: tuple[str, ...] = ("bugfix",),
    max_fragments: int | None = None,
    max_gap_days: int = MAX_GAP_DAYS,
    commit_to_pr=None,
    get_endpoints=None,
    report_ts_for=None,
    workers: int = 1,
    progress: int = 0,
) -> tuple[list[ChainEdge], dict[int, PullRequest], dict]:
    """(edges, supplemental PRs, stats) from fixers + SZZ."""
    if spec is None:
        from ..langspec import PYTHON
        spec = PYTHON
    anchor2pr = {pr.anchor_sha: n for n, pr in pr_index.items()}
    supplemental: dict[int, PullRequest] = {}
    # guards `supplemental`; fetch stays unlocked
    _supp_lock = threading.Lock()

    def _add_supplemental(n: int) -> bool:
        with _supp_lock:
            if n in pr_index or n in supplemental:
                return True
        if get_endpoints is None:
            return False
        ep = get_endpoints(n)
        if not ep:
            return False
        with _supp_lock:
            supplemental.setdefault(n, PullRequest(
                number=n, anchor_sha=ep["head_sha"], base_sha=ep["base_sha"],
                head_sha=ep["head_sha"], kind="api", date=ep["date"],
                subject=ep["title"], message=""))
        return True

    def resolve(sha: str) -> int | None:
        """Commit -> PR number."""
        n = _pr_of_commit(repo, sha, anchor2pr)
        if n is not None:
            return n
        if commit_to_pr is None:
            return None
        n = commit_to_pr(sha)
        if n is None:
            return None
        return n if _add_supplemental(n) else None

    def _pr(n):
        with _supp_lock:
            return pr_index.get(n) or supplemental.get(n)

    # 1) collect fixer PRs
    if fixer_prs:
        # from bug labels
        ordered = sorted(fixer_prs, reverse=True)
        if max_fragments:
            ordered = ordered[:max_fragments]
        fixers = [(n if _add_supplemental(n) else None, f"label #{n}")
                  for n in ordered]
        n_fragments = len(fixer_prs)
    elif changelog_dir:
        frags = find_bugfix_fragments(repo, changelog_dir, categories)
        ordered = sorted(frags, key=lambda n: int(n), reverse=True)
        if max_fragments:
            ordered = ordered[:max_fragments]
        fixers = [(resolve(frags[num]), f"fragment #{num}") for num in ordered]
        n_fragments = len(frags)
    elif changelog_file:
        prs = find_bugfix_prs_changelog(repo, changelog_file)
        ordered = sorted(prs, reverse=True)
        if max_fragments:
            ordered = ordered[:max_fragments]
        fixers = [(n if _add_supplemental(n) else None,
                   f"changelog :pull: #{n}") for n in ordered]
        n_fragments = len(prs)
    elif changelog_glob:
        # matplotlib github_stats
        prs = find_bugfix_prs_ghstats(repo, changelog_glob)
        ordered = sorted(prs, reverse=True)
        if max_fragments:
            ordered = ordered[:max_fragments]
        fixers = [(n if _add_supplemental(n) else None,
                   f"github_stats :ghpull: #{n}") for n in ordered]
        n_fragments = len(prs)
    elif bugfix_commit_re:
        # bugfix commit subjects
        shas = find_bugfix_commits(repo, bugfix_commit_re,
                                   since=bugfix_commit_since)
        if max_fragments:
            shas = shas[:max_fragments]
        fixers = [(resolve(sha), f"bugfix commit {sha[:10]}") for sha in shas]
        n_fragments = len(shas)
    else:
        raise ValueError("changelog mining needs fixer_prs, changelog_dir, "
                         "changelog_file, changelog_glob, or bugfix_commit_re")

    # 2) blame each fixer for its introducer
    edges: dict[tuple[int, int], ChainEdge] = {}
    stats = {"fragments": n_fragments, "fixer_resolved": 0,
             "fixer_via_api": 0, "introducer_found": 0, "dropped_gap": 0,
             "report_dated": 0, "no_introducer_predates_report": 0}
    todo = [(n, label) for n, label in fixers if n is not None]
    # record before blaming adds introducers
    via_api = {n for n, _ in todo if n in supplemental}
    done = 0
    prog_lock = threading.Lock()

    def _blame_one(item):
        """Blame one chain."""
        nonlocal done
        pr2_num, label = item
        rts = report_ts_for(pr2_num) if report_ts_for else None
        pr1_num = szz_introducer(repo, _pr(pr2_num), resolve,
                                 spec.is_source, spec.is_blamable, report_ts=rts)
        if progress:
            with prog_lock:
                done += 1
                if done % progress == 0 or done == len(todo):
                    print(f"[szz] blamed {done}/{len(todo)} chains",
                          file=sys.stderr, flush=True)
        return pr2_num, label, rts, pr1_num

    # map keeps input order
    if workers > 1 and len(todo) > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_blame_one, todo))
    else:
        results = [_blame_one(item) for item in todo]

    for pr2_num, label, rts, pr1_num in results:
        stats["fixer_resolved"] += 1
        if pr2_num in via_api:
            stats["fixer_via_api"] += 1
        if rts is not None:
            stats["report_dated"] += 1
        if pr1_num is None or pr1_num == pr2_num or pr1_num >= pr2_num:
            if rts is not None and pr1_num is None:
                # all candidates after the report
                stats["no_introducer_predates_report"] += 1
            continue
        if _pr(pr1_num) is None:
            continue
        # drop long gaps
        gap = _gap_days(_pr(pr1_num).date, _pr(pr2_num).date)
        if max_gap_days and gap is not None and gap > max_gap_days:
            stats["dropped_gap"] += 1
            continue
        stats["introducer_found"] += 1
        key = (pr1_num, pr2_num)
        edges.setdefault(key, ChainEdge(
            introducer=pr1_num, fixer=pr2_num, category="bugfix",
            phrase=f"{label} + SZZ blame",
            source="label_szz" if fixer_prs else "changelog_szz"))
    return list(edges.values()), supplemental, stats
