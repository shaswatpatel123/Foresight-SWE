"""Dataclasses shared across the mining pipeline."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any


@dataclass
class Commit:
    sha: str
    parents: list[str]
    date: str          # author date, ISO-8601
    subject: str
    body: str

    @property
    def message(self) -> str:
        return f"{self.subject}\n{self.body}".strip()


@dataclass
class PullRequest:
    """A PR rebuilt from git history."""

    number: int
    anchor_sha: str
    base_sha: str
    head_sha: str
    kind: str          # "squash" | "merge"
    date: str
    subject: str
    message: str = ""  # text scanned for references

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Reference:
    """A `#N` reference in PR text."""

    source_pr: int
    target_number: int
    category: str      # see references.CATEGORIES
    phrase: str
    source_sha: str
    target_is_known_pr: bool


# categories that form a fix-chain
CHAIN_CATEGORIES = {"regression", "introduced", "followup", "supersedes", "partial"}


@dataclass
class ChainEdge:
    """An introducer -> fixer edge."""

    introducer: int
    fixer: int
    category: str
    phrase: str
    source: str  # where the edge came from


@dataclass
class Chain:
    repo: str
    earlier_pr: int    # PR1, the introducer
    later_pr: int      # PR2, the fix
    category: str
    evidence: str      # matched phrase
    pr1_base_sha: str  # agent starts here
    pr1_head_sha: str  # after PR1
    pr2_base_sha: str
    pr2_head_sha: str  # after PR2
    pr1_date: str = ""
    pr2_date: str = ""
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
