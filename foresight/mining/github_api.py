"""Mine chain edges from the GitHub API (cached)."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config
from .models import ChainEdge
from .references import extract_references

API = "https://api.github.com"

# without a timeout, stalls hang forever
REQUEST_TIMEOUT = 30.0


class GitHubClient:
    def __init__(self, token: str, repo_name: str):
        self.token = token
        self.cache_dir = config.DATA_DIR / "github_cache" / repo_name
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()[:16]
        return self.cache_dir / f"{h}.json"

    def get(self, path: str, *, params: dict | None = None, allow_404: bool = False):
        """Cached GET; None on 404 if allowed."""
        query = ""
        if params:
            from urllib.parse import urlencode
            query = "?" + urlencode(params)
        url = f"{API}{path}{query}"
        cache = self._cache_path(url)
        if cache.exists():
            return json.loads(cache.read_text())

        for attempt in range(6):
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", "foresightCoder-miner")
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                    cache.write_text(json.dumps(data))
                    if remaining is not None and int(remaining) == 0:
                        self._sleep_until(resp.headers.get("X-RateLimit-Reset"))
                    return data
            except urllib.error.HTTPError as exc:
                # 410 Gone: treat like 404
                if exc.code in (404, 410) and allow_404:
                    cache.write_text("null")
                    return None
                if exc.code in (403, 429):  # rate / secondary limit
                    reset = exc.headers.get("X-RateLimit-Reset")
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after:
                        time.sleep(min(int(retry_after), 120))
                    else:
                        self._sleep_until(reset)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # network error: safe to retry
                if attempt == 5:
                    raise RuntimeError(f"network error on {url}: {exc}") from exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"gave up on {url}")

    def graphql(self, query: str):
        """Cached GraphQL POST."""
        cache = self._cache_path("GRAPHQL:" + query)
        if cache.exists():
            return json.loads(cache.read_text())
        body = json.dumps({"query": query}).encode()
        for attempt in range(6):
            req = urllib.request.Request(f"{API}/graphql", data=body)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", "foresightCoder-miner")
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                # never cache errors
                if "errors" not in data:
                    cache.write_text(json.dumps(data))
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    self._sleep_until(exc.headers.get("X-RateLimit-Reset"))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 5:
                    raise RuntimeError(f"network error on graphql: {exc}") from exc
                time.sleep(2 ** attempt)
        raise RuntimeError("gave up on graphql")

    def search_issues(self, q: str, *, max_pages: int = 10) -> list[dict]:
        """Paged issue search (max 1000)."""
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            d = self.get("/search/issues",
                         params={"q": q, "per_page": 100, "page": page})
            items = (d or {}).get("items") or []
            out.extend(items)
            if len(items) < 100:
                break
        return out

    @staticmethod
    def _sleep_until(reset_epoch: str | None) -> None:
        if not reset_epoch:
            time.sleep(60)
            return
        wait = max(0, int(reset_epoch) - int(time.time())) + 2
        time.sleep(min(wait, 3600))

    # --- domain helpers -------------------------------------------------

    def is_pr(self, slug: str, number: int) -> bool | None:
        """True: PR, False: issue, None: missing."""
        data = self.get(f"/repos/{slug}/issues/{number}", allow_404=True)
        if data is None:
            return None
        return "pull_request" in data

    def recent_merged_prs(self, slug: str, since_iso: str, max_prs: int) -> list[dict]:
        """Merged PRs since a date, newest first."""
        out: list[dict] = []
        page = 1
        while len(out) < max_prs:
            batch = self.get(
                f"/repos/{slug}/pulls",
                params={"state": "closed", "sort": "created", "direction": "desc",
                        "per_page": "100", "page": str(page)},
            )
            if not batch:
                break
            stop = False
            for pr in batch:
                if pr.get("created_at", "") < since_iso:
                    stop = True
                    break
                if pr.get("merged_at"):
                    out.append(pr)
            if stop or len(batch) < 100:
                break
            page += 1
        return out[:max_prs]

    def issue_comments(self, slug: str, number: int) -> list[dict]:
        data = self.get(f"/repos/{slug}/issues/{number}/comments",
                        params={"per_page": "100"}, allow_404=True)
        return data or []

    def pr_for_commit(self, slug: str, sha: str) -> int | None:
        """PR containing a commit; prefer merged."""
        data = self.get(f"/repos/{slug}/commits/{sha}/pulls", allow_404=True)
        if not data:
            return None
        merged = [p for p in data if p.get("merged_at")]
        chosen = (merged or data)[0]
        return chosen.get("number")

    def pr_endpoints(self, slug: str, number: int) -> dict | None:
        """PR base and merge commit."""
        pr = self.get(f"/repos/{slug}/pulls/{number}", allow_404=True)
        if not pr:
            return None
        base = (pr.get("base") or {}).get("sha")
        head = pr.get("merge_commit_sha") or (pr.get("head") or {}).get("sha")
        if not (base and head):
            return None
        return {"base_sha": base, "head_sha": head,
                "title": pr.get("title", ""), "date": pr.get("merged_at", "") or ""}


from .models import CHAIN_CATEGORIES  # noqa: E402

# "fixes #N" links in PR bodies
import re  # noqa: E402

_CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)


def find_chain_edges(
    client: GitHubClient,
    slug: str,
    *,
    since_iso: str,
    max_prs: int = 1500,
    deep: bool = True,
) -> list[ChainEdge]:
    """Edges from fix phrases in PR and issue text."""
    prs = client.recent_merged_prs(slug, since_iso, max_prs)
    edges: dict[tuple[int, int], ChainEdge] = {}
    pr_cache: dict[int, bool | None] = {}

    def resolve_is_pr(n: int) -> bool | None:
        if n not in pr_cache:
            pr_cache[n] = client.is_pr(slug, n)
        return pr_cache[n]

    def consider(fixer: int, text: str, source: str) -> None:
        for number, category, phrase in extract_references(text or ""):
            if category not in CHAIN_CATEGORIES or number == fixer or number >= fixer:
                continue  # need an earlier PR
            if resolve_is_pr(number) is not True:
                continue
            key = (number, fixer)
            if key not in edges:
                edges[key] = ChainEdge(number, fixer, category, phrase, source)

    for pr in prs:
        fixer = pr["number"]
        consider(fixer, f"{pr.get('title','')}\n{pr.get('body','') or ''}", "pr_body")
        if deep:
            body = pr.get("body", "") or ""
            for issue_n in {int(m) for m in _CLOSES_RE.findall(body)}:
                if resolve_is_pr(issue_n) is not False:
                    continue  # issues only
                issue = client.get(f"/repos/{slug}/issues/{issue_n}", allow_404=True)
                if issue:
                    consider(fixer, issue.get("body", "") or "", "issue_body")
                for c in client.issue_comments(slug, issue_n):
                    consider(fixer, c.get("body", "") or "", "issue_comment")
    return list(edges.values())


# --- fixer PRs from bug labels (PR or issue)

def merged_prs_with_label(client: GitHubClient, slug: str, labels: tuple[str, ...],
                          *, since: str = "", max_pages: int = 10) -> set[int]:
    """Merged PRs with a bug label."""
    found: set[int] = set()
    for lab in labels:
        q = f'repo:{slug} is:pr is:merged label:"{lab}"'
        if since:
            q += f" merged:>{since}"
        for it in client.search_issues(q, max_pages=max_pages):
            found.add(it["number"])
    return found


def prs_closing_labeled_issues(client: GitHubClient, slug: str,
                               labels: tuple[str, ...], *, since: str = "",
                               batch: int = 50, max_pages: int = 10) -> set[int]:
    """Merged PRs that closed bug-labelled issues."""
    owner, name = slug.split("/", 1)
    nums: list[int] = []
    for lab in labels:
        q = f'repo:{slug} is:issue is:closed label:"{lab}"'
        if since:
            q += f" closed:>{since}"
        nums += [it["number"] for it in client.search_issues(q, max_pages=max_pages)]

    found: set[int] = set()
    for i in range(0, len(nums), batch):
        chunk = nums[i:i + batch]
        body = "\n".join(
            f"  a{n}: issue(number:{n}) {{ closedByPullRequestsReferences"
            f"(first:3, includeClosedPrs:true) {{ nodes {{ number merged }} }} }}"
            for n in chunk)
        d = client.graphql(
            f'query {{ repository(owner:"{owner}", name:"{name}") {{\n{body}\n}} }}')
        repo_node = (d.get("data") or {}).get("repository") or {}
        for v in repo_node.values():
            if not v:
                continue
            for p in (v.get("closedByPullRequestsReferences") or {}).get("nodes", []):
                if p.get("merged"):
                    found.add(p["number"])
    return found
