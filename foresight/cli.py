"""Command-line entry point: foresight <command> ..."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from . import config
from . import validate as validate_mod
from .mining import chains as chains_mod
from .mining import git_source


def _cmd_mine(args: argparse.Namespace) -> int:
    target = config.get_target(args.repo)
    if not target.path.exists():
        raise SystemExit(
            f"repo not cloned: {target.path}\n"
            f"  git clone https://github.com/{target.slug}.git {target.path}"
        )

    branch = args.branch or target.main_branch

    # Django has no PRs: own miner
    if target.runner == "django":
        from .mining import django_szz
        since = args.since or "2023-01-01"
        print(f"[mine] django commit-based mining {target.slug} @ {branch} "
              f"(Fixed #N + SZZ) since {since} ...", file=sys.stderr)
        records, stats = django_szz.mine_chains(target.path, ref=branch, since=since)
        config.CHAINS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = args.out or (config.CHAINS_DIR / f"{target.name}_candidates.jsonl")
        with open(out_path, "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        summary = {"repo": target.name, "django_chains": len(records), **stats}
        with open(config.CHAINS_DIR / f"{target.name}_summary.json", "w") as fh:
            json.dump(summary, fh, indent=2)
        print(json.dumps(summary, indent=2))
        print(f"\n[mine] {len(records)} candidate chains -> {out_path}", file=sys.stderr)
        return 0

    print(f"[mine] reading {target.slug} @ {branch} ...", file=sys.stderr)
    commits = git_source.iter_commits(target.path, branch)
    print(f"[mine] {len(commits)} commits", file=sys.stderr)

    pr_index = git_source.build_pr_index(commits)
    if not args.no_merge_text:
        enriched = git_source.attach_merge_branch_text(commits, pr_index)
        print(f"[mine] enriched {enriched} merge PRs with branch text", file=sys.stderr)
    references = chains_mod.find_references(pr_index)
    git_chains = chains_mod.build_chains(target.name, pr_index, references)

    api_chains: list = []
    if args.use_api:
        from .mining import github_api
        from . import secrets
        token = secrets.github_token()
        if not token:
            raise SystemExit("--use-api needs a token (GITHUB_TOKEN or .github_token)")
        since = args.since or (
            datetime.now(timezone.utc) - timedelta(days=365 * args.years)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[mine] API mining {target.slug} merged PRs since {since} ...", file=sys.stderr)
        client = github_api.GitHubClient(token, target.name)
        edges = github_api.find_chain_edges(
            client, target.slug, since_iso=since,
            max_prs=args.max_prs, deep=not args.no_deep,
        )
        api_chains, unresolved = chains_mod.build_chains_from_edges(
            target.name, pr_index, edges)
        print(f"[mine] API: {len(edges)} edges -> {len(api_chains)} chains "
              f"({unresolved} unresolved against local clone)", file=sys.stderr)

    cl_chains: list = []
    if args.use_changelog:
        if not (target.changelog_dir or target.changelog_file or target.changelog_glob
                or target.bugfix_commit_re):
            raise SystemExit(
                f"--use-changelog: no changelog_dir/changelog_file/changelog_glob/"
                f"bugfix_commit_re configured for {target.name}")
        from .mining import changelog_szz
        from . import secrets
        commit_to_pr = get_endpoints = None
        token = secrets.github_token()
        if token:
            from .mining import github_api
            client = github_api.GitHubClient(token, target.name)
            commit_to_pr = lambda sha: client.pr_for_commit(target.slug, sha)
            get_endpoints = lambda n: client.pr_endpoints(target.slug, n)
            print("[mine] CHANGELOG: API commit->PR resolution ON "
                  "(recovers rebase-merged PRs)", file=sys.stderr)
        else:
            print("[mine] CHANGELOG: local-only resolution (no token; "
                  "rebase-merged PRs will be missed)", file=sys.stderr)
        src = (target.changelog_dir or target.changelog_file or target.changelog_glob
               or f"commit subjects /{target.bugfix_commit_re}/")
        print(f"[mine] CHANGELOG+SZZ mining {target.name} "
              f"({src}) ...", file=sys.stderr)
        edges, supplemental, cl_stats = changelog_szz.find_chain_edges(
            target.path, pr_index, target.langspec,
            changelog_dir=target.changelog_dir,
            changelog_file=target.changelog_file,
            changelog_glob=target.changelog_glob,
            bugfix_commit_re=target.bugfix_commit_re,
            bugfix_commit_since=args.commit_since,
            categories=target.changelog_bugfix_cats,
            max_fragments=args.max_fragments,
            commit_to_pr=commit_to_pr, get_endpoints=get_endpoints,
            workers=args.workers, progress=25,
        )
        cl_index = {**pr_index, **supplemental}
        cl_chains, cl_unresolved = chains_mod.build_chains_from_edges(
            target.name, cl_index, edges)
        print(f"[mine] CHANGELOG: {cl_stats} -> {len(cl_chains)} chains "
              f"(+{len(supplemental)} PRs recovered via API; "
              f"{cl_unresolved} unresolved)", file=sys.stderr)

    chains = chains_mod.merge_chains(git_chains, api_chains, cl_chains)
    summary = chains_mod.summarize(pr_index, references, chains)
    summary["git_chains"] = len(git_chains)
    summary["api_chains"] = len(api_chains)
    summary["changelog_chains"] = len(cl_chains)

    config.CHAINS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (config.CHAINS_DIR / f"{target.name}_candidates.jsonl")
    with open(out_path, "w") as fh:
        for row in chains_mod.iter_chain_dicts(chains):
            fh.write(json.dumps(row) + "\n")
    summary_path = config.CHAINS_DIR / f"{target.name}_summary.json"
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"\n[mine] {len(chains)} candidate chains -> {out_path}", file=sys.stderr)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    target = config.get_target(args.repo)
    if not target.path.exists():
        raise SystemExit(f"repo not cloned: {target.path}")

    candidates_path = args.candidates or (
        config.CHAINS_DIR / f"{target.name}_candidates.jsonl"
    )
    chains = [json.loads(line) for line in open(candidates_path) if line.strip()]
    # recent PRs first
    chains.sort(key=lambda c: c["later_pr"], reverse=True)
    if args.min_pr:
        chains = [c for c in chains if c["later_pr"] >= args.min_pr]
    if args.limit:
        chains = chains[: args.limit]

    run_kw = {"python": target.python, "install_args": target.install_args,
              "clear_addopts": target.clear_addopts}
    results = []
    status_counts: dict[str, int] = {}
    for i, chain in enumerate(chains, 1):
        label = f"PR{chain['earlier_pr']}->PR{chain['later_pr']}"
        print(f"[validate {i}/{len(chains)}] {label} ...", file=sys.stderr)
        res = validate_mod.validate_chain(target.path, target.name, chain, **run_kw)
        results.append(res)
        status_counts[res["status"]] = status_counts.get(res["status"], 0) + 1
        n = len(res["anticipation_tests"])
        print(f"    -> {res['status']}"
              + (f" ({n} anticipation test(s))" if n else ""), file=sys.stderr)

    config.CHAINS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (config.CHAINS_DIR / f"{target.name}_validated.jsonl")
    with open(out_path, "w") as fh:
        for res in results:
            fh.write(json.dumps(res) + "\n")

    summary = {
        "chains_attempted": len(results),
        "status_counts": status_counts,
        "validated": sum(1 for r in results if r["status"] == "validated"),
        "total_anticipation_tests": sum(len(r["anticipation_tests"]) for r in results),
    }
    print(json.dumps(summary, indent=2))
    print(f"\n[validate] -> {out_path}", file=sys.stderr)
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    from . import dataset as dataset_mod
    from . import inspect as inspect_mod
    rows = dataset_mod.load_validated()
    report = inspect_mod.build_report(rows)
    out = args.out or str(config.DATA_DIR / "review.md")
    with open(out, "w") as fh:
        fh.write(report)
    print(f"[inspect] review for {len(rows)} chains -> {out}", file=sys.stderr)
    return 0


def _cmd_mutate(args: argparse.Namespace) -> int:
    from . import dataset as dataset_mod
    from . import mutation as mutation_mod
    rows = dataset_mod.apply_quality(dataset_mod.load_validated())
    if args.quality != "all":
        rows = [r for r in rows if r.get("quality") == args.quality]
    if args.limit:
        rows = rows[: args.limit]
    results = []
    for i, chain in enumerate(rows, 1):
        target = config.get_target(chain["repo"])
        print(f"[mutate {i}/{len(rows)}] {chain['repo']} PR{chain['later_pr']} ...",
              file=sys.stderr)
        res = mutation_mod.check_chain(
            chain, python=target.python, install_args=target.install_args,
            max_mutants=args.max_mutants)
        results.append(res)
        print(f"    score={res['mutation_score']} "
              f"(killed {res['killed']}/{res['killed'] + res['survived']}, "
              f"{res['n_mutants']} mutants)", file=sys.stderr)
    out = config.DATA_DIR / "mutation_results.json"
    out.write_text(json.dumps(results, indent=2))
    scored = [r for r in results if r["mutation_score"] is not None]
    avg = round(sum(r["mutation_score"] for r in scored) / len(scored), 2) if scored else None
    weak = [f"{r['repo']}/PR{r['later_pr']}" for r in scored if r["mutation_score"] == 0]
    print(json.dumps({"chains": len(results), "scored": len(scored),
                      "avg_mutation_score": avg, "zero_score_chains": weak}, indent=2))
    return 0


def _cmd_dataset(args: argparse.Namespace) -> int:
    from . import dataset as dataset_mod
    rows = dataset_mod.load_validated()
    rows = dataset_mod.apply_quality(rows)
    out = dataset_mod.write_dataset(rows)
    print(json.dumps(dataset_mod.stats(rows), indent=2))
    print(f"\n[dataset] {len(rows)} validated chains -> {out}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foresight")
    sub = parser.add_subparsers(dest="command", required=True)

    mine = sub.add_parser("mine", help="mine candidate fix-chains from git history")
    mine.add_argument("--repo", required=True, help="target name (e.g. pytest)")
    mine.add_argument("--branch", default=None, help="branch to mine (default: target main)")
    mine.add_argument("--out", default=None, help="output JSONL path")
    mine.add_argument(
        "--no-merge-text",
        action="store_true",
        help="skip walking merge-PR branch commits for cross-references",
    )
    mine.add_argument("--use-api", action="store_true",
                      help="also mine PR/issue text via the GitHub API")
    mine.add_argument("--since", default=None,
                      help="ISO date floor for API mining (default: --years back)")
    mine.add_argument("--years", type=int, default=2,
                      help="API: how many years of recent PRs to scan (default 2)")
    mine.add_argument("--max-prs", type=int, default=1500,
                      help="API: cap on PRs scanned (default 1500)")
    mine.add_argument("--no-deep", action="store_true",
                      help="API: skip linked-issue bodies/comments (PR body only)")
    mine.add_argument("--use-changelog", action="store_true",
                      help="also mine bugfix-chain edges from CHANGELOG fragments + SZZ blame")
    mine.add_argument("--commit-since", default="",
                      help="bound the bugfix-COMMIT fixer source by date "
                           "(e.g. 2018-08-03). Safe vs the recency tier: a "
                           "fixer older than N years implies an older "
                           "introducer, so it cannot drop a tier<=N chain.")
    mine.add_argument("--max-fragments", type=int, default=None,
                      help="CHANGELOG: cap fragments scanned (newest first)")
    mine.add_argument("--workers", type=int, default=1,
                      help="CHANGELOG+SZZ: blame this many chains concurrently. "
                           "SZZ only reads git history (no checkout), so this is "
                           "safe; the result is identical to --workers 1. Worth "
                           "raising on deep-history repos (e.g. svelte).")
    mine.set_defaults(func=_cmd_mine)

    val = sub.add_parser("validate", help="validate candidate chains by execution")
    val.add_argument("--repo", required=True, help="target name (e.g. pytest)")
    val.add_argument("--candidates", default=None, help="candidates JSONL path")
    val.add_argument("--out", default=None, help="output JSONL path")
    val.add_argument("--limit", type=int, default=None, help="max chains to attempt")
    val.add_argument("--min-pr", type=int, default=None,
                     help="only chains whose later_pr >= this (recent = installable)")
    val.set_defaults(func=_cmd_validate)

    ds = sub.add_parser("dataset", help="consolidate validated chains + stats")
    ds.set_defaults(func=_cmd_dataset)

    insp = sub.add_parser("inspect", help="write a per-chain review report")
    insp.add_argument("--out", default=None, help="output markdown path")
    insp.set_defaults(func=_cmd_inspect)

    mut = sub.add_parser("mutate", help="mutation-validity check on anticipation tests")
    mut.add_argument("--quality", default="clean", help="only chains with this quality (or 'all')")
    mut.add_argument("--limit", type=int, default=None, help="max chains")
    mut.add_argument("--max-mutants", type=int, default=4, help="mutants per chain")
    mut.set_defaults(func=_cmd_mutate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
