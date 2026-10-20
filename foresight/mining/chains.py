"""Turn references into candidate chains."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

from .models import CHAIN_CATEGORIES, Chain, PullRequest, Reference
from .references import extract_references


def find_references(pr_index: dict[int, PullRequest]) -> list[Reference]:
    refs: list[Reference] = []
    for pr in pr_index.values():
        for number, category, phrase in extract_references(pr.message):
            if number == pr.number:
                continue  # self-reference
            refs.append(
                Reference(
                    source_pr=pr.number,
                    target_number=number,
                    category=category,
                    phrase=phrase,
                    source_sha=pr.anchor_sha,
                    target_is_known_pr=number in pr_index,
                )
            )
    return refs


def chain_from_prs(
    repo_name: str,
    pr1: PullRequest,
    pr2: PullRequest,
    category: str,
    evidence: str,
    extra_flags: list[str] | None = None,
) -> Chain:
    flags = list(extra_flags or [])
    # PR1 must predate PR2
    if pr1.date and pr2.date and pr1.date > pr2.date:
        flags.append("date_inversion")
    return Chain(
        repo=repo_name,
        earlier_pr=pr1.number,
        later_pr=pr2.number,
        category=category,
        evidence=evidence,
        pr1_base_sha=pr1.base_sha,
        pr1_head_sha=pr1.head_sha,
        pr2_base_sha=pr2.base_sha,
        pr2_head_sha=pr2.head_sha,
        pr1_date=pr1.date,
        pr2_date=pr2.date,
        flags=flags,
    )


def build_chains(
    repo_name: str,
    pr_index: dict[int, PullRequest],
    references: list[Reference],
) -> list[Chain]:
    """Chains from fix references to known PRs."""
    chains: dict[tuple[int, int, str], Chain] = {}
    for ref in references:
        if ref.category not in CHAIN_CATEGORIES:
            continue
        if not ref.target_is_known_pr:
            continue
        pr2 = pr_index[ref.source_pr]   # the fixer
        pr1 = pr_index[ref.target_number]  # the introducer
        if pr1.number == pr2.number:
            continue
        key = (pr1.number, pr2.number, ref.category)
        if key not in chains:
            chains[key] = chain_from_prs(repo_name, pr1, pr2, ref.category, ref.phrase)
    return list(chains.values())


def build_chains_from_edges(repo_name, pr_index, edges):
    """Chains from (introducer, fixer) edges."""
    chains: dict[tuple[int, int, str], Chain] = {}
    unresolved = 0
    for edge in edges:
        pr1 = pr_index.get(edge.introducer)
        pr2 = pr_index.get(edge.fixer)
        if pr1 is None or pr2 is None:
            unresolved += 1
            continue
        key = (pr1.number, pr2.number, edge.category)
        if key not in chains:
            chains[key] = chain_from_prs(
                repo_name, pr1, pr2, edge.category, edge.phrase,
                extra_flags=[f"src:{edge.source}"],
            )
    return list(chains.values()), unresolved


def merge_chains(*chain_lists: list[Chain]) -> list[Chain]:
    """Dedupe chains; first one wins."""
    seen: dict[tuple[int, int], Chain] = {}
    for chains in chain_lists:
        for c in chains:
            seen.setdefault((c.earlier_pr, c.later_pr), c)
    return list(seen.values())


def summarize(
    pr_index: dict[int, PullRequest],
    references: list[Reference],
    chains: list[Chain],
) -> dict:
    ref_by_cat = Counter(r.category for r in references)
    chain_by_cat = Counter(c.category for c in chains)
    resolved = sum(1 for r in references if r.target_is_known_pr)
    clean = [c for c in chains if not c.flags]
    return {
        "prs_indexed": len(pr_index),
        "pr_kinds": dict(Counter(p.kind for p in pr_index.values())),
        "references_found": len(references),
        "references_resolved_to_known_pr": resolved,
        "references_by_category": dict(ref_by_cat),
        "candidate_chains": len(chains),
        "chains_without_flags": len(clean),
        "chains_by_category": dict(chain_by_cat),
    }


def iter_chain_dicts(chains: list[Chain]) -> Iterator[dict]:
    for chain in chains:
        yield chain.to_dict()
