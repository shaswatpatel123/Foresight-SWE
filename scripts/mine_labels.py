#!/usr/bin/env python3
"""Mine chains from bug labels on PRs or issues."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config, secrets
from foresight.langspec import get_spec
from foresight.mining import changelog_szz, git_source, github_api
from foresight.mining import chains as chains_mod
from foresight.mining.chains import build_chains_from_edges


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--mode", choices=["pr", "issue"], required=True)
    ap.add_argument("--labels", required=True, help="comma list, e.g. 'bug,Crash'")
    ap.add_argument("--since", default="", help="YYYY-MM-DD bound on merge/close date")
    ap.add_argument("--max-pages", type=int, default=10,
                    help="search pages per label (GitHub caps this endpoint at 10 "
                         "x 100 = 1000 results; slice --since to go deeper)")
    ap.add_argument("--limit", type=int, default=0, help="cap fixers (0 = all)")
    ap.add_argument("--workers", type=int, default=1,
                    help="SZZ blame concurrency. Blame only READS git history, so "
                         "the output is identical to --workers 1. Deep-history "
                         "repos want this high.")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t = config.get_target(args.repo)
    if not t.path.exists():
        sys.exit(f"clone missing: git clone https://github.com/{t.slug} {t.path}")
    labels = tuple(l.strip() for l in args.labels.split(",") if l.strip())
    client = github_api.GitHubClient(secrets.github_token(), args.repo)

    print(f"{args.repo}: gathering fixer PRs by label {labels} (mode={args.mode})",
          flush=True)
    if args.mode == "pr":
        fixers = github_api.merged_prs_with_label(
            client, t.slug, labels, since=args.since, max_pages=args.max_pages)
    else:
        fixers = github_api.prs_closing_labeled_issues(
            client, t.slug, labels, since=args.since, max_pages=args.max_pages)
    print(f"  fixer PRs found: {len(fixers)}", flush=True)
    if not fixers:
        sys.exit("no fixer PRs -- wrong label name? check the repo's label list")

    print("  building local PR index...", flush=True)
    commits = git_source.iter_commits(t.path, t.main_branch)
    prs = git_source.build_pr_index(commits)
    print(f"  local PR index: {len(prs)} PRs from {len(commits)} commits", flush=True)

    edges, supplemental, stats = changelog_szz.find_chain_edges(
        t.path, prs, get_spec(t.language),
        fixer_prs=fixers,
        max_fragments=args.limit or None,
        commit_to_pr=lambda sha: client.pr_for_commit(t.slug, sha),
        get_endpoints=lambda n: client.pr_endpoints(t.slug, n),
        workers=args.workers, progress=25,
    )
    prs.update(supplemental)
    print(f"  stats: {stats}", flush=True)
    print(f"  EDGES: {len(edges)}", flush=True)

    # unresolved edges are reported
    chains, unresolved = build_chains_from_edges(args.repo, prs, edges)
    rows = list(chains_mod.iter_chain_dicts(chains))
    out = (Path(args.out) if args.out
           else config.CHAINS_DIR / f"{args.repo}_label_candidates.jsonl")
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"  unresolved edges (PRs absent from the local index): {unresolved}")
    print(f"\nCHAINS: {len(rows)} -> {out}")


if __name__ == "__main__":
    main()
