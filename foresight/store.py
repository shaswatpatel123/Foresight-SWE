"""Read and write the chain master table."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config

MASTER = config.DATA_DIR / "chains_master.jsonl"
TEST_LISTS = config.DATA_DIR / "test_lists.jsonl"
SNAPSHOT_DIR = config.DATA_DIR / "_snapshots"

# blocks a stage may own
BLOCKS = frozenset({
    "excluded", "filters", "install", "judge", "plan", "published",
    "sources", "szz", "validation", "verifications",
})

# identity fields, set only by mining
IDENTITY = frozenset({
    "instance_id", "repo", "language", "category", "introducer_pr", "fixer_pr",
    "pr1_base_sha", "pr1_head_sha", "pr2_base_sha", "pr2_head_sha",
    "pr1_date", "pr2_date", "introducer_found_by", "date_inversion",
})


class StoreError(RuntimeError):
    """A write would break ownership."""


# --------------------------------------------------------------------------- io

def dumps(row: dict) -> str:
    """Canonical one-line form of a row."""
    return json.dumps(row, sort_keys=True) + "\n"


def load(path: Path | str = MASTER) -> list[dict]:
    """All rows, in file order."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def index(rows: Iterable[dict]) -> dict[str, dict]:
    """`{instance_id: row}`; raises on duplicates."""
    out: dict[str, dict] = {}
    for row in rows:
        rid = row["instance_id"]
        if rid in out:
            raise StoreError(f"duplicate instance_id in store: {rid}")
        out[rid] = row
    return out


def save(rows: Iterable[dict], path: Path | str = MASTER, *, snapshot: bool = True) -> int:
    """Write rows atomically; returns the count."""
    path = Path(path)
    rows = sorted(rows, key=lambda r: r["instance_id"])
    seen = set()
    for row in rows:
        if row["instance_id"] in seen:
            raise StoreError(f"duplicate instance_id on save: {row['instance_id']}")
        seen.add(row["instance_id"])

    if snapshot and path.exists():
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        n = 1 + max((int(p.stem.rsplit(".", 1)[-1])
                     for p in SNAPSHOT_DIR.glob(f"{path.stem}.*.jsonl")
                     if p.stem.rsplit(".", 1)[-1].isdigit()), default=0)
        shutil.copy2(path, SNAPSHOT_DIR / f"{path.stem}.{n:04d}.jsonl")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for row in rows:
                fh.write(dumps(row))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(rows)


# ------------------------------------------------------------------- ownership

@dataclass
class UpdateReport:
    """What one block write did."""

    block: str
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        bits = [f"{self.block}: {len(self.changed)} changed",
                f"{len(self.unchanged)} unchanged"]
        if self.added:
            bits.append(f"{len(self.added)} new rows")
        if self.unknown:
            bits.append(f"{len(self.unknown)} ids NOT in store")
        return " · ".join(bits)


def _without(row: dict, block: str) -> str:
    return json.dumps({k: v for k, v in row.items() if k != block}, sort_keys=True)


def update(block: str, updates: dict[str, Any], *, path: Path | str = MASTER,
           rows: list[dict] | None = None, dry_run: bool = False,
           strict: bool = True) -> UpdateReport:
    """Write one block; nothing else may change."""
    if block not in BLOCKS:
        raise StoreError(f"{block!r} is not a stage block; expected one of {sorted(BLOCKS)}")

    # baseline comes from disk, not `rows`
    before = {row["instance_id"]: _without(row, block) for row in load(path)}
    by_id = index(load(path) if rows is None else rows)
    report = UpdateReport(block=block)

    if set(by_id) != set(before):
        gone, extra = sorted(set(before) - set(by_id)), sorted(set(by_id) - set(before))
        raise StoreError(
            f"the rows passed in do not match {Path(path).name}: "
            f"{len(gone)} missing (e.g. {gone[:3]}), {len(extra)} new (e.g. {extra[:3]}). "
            f"Stages update rows; only mining adds or removes them.")

    for rid, value in updates.items():
        row = by_id.get(rid)
        if row is None:
            report.unknown.append(rid)
            continue
        if row.get(block) == value:
            report.unchanged.append(rid)
            continue
        row[block] = value
        report.changed.append(rid)

    if report.unknown and strict:
        raise StoreError(
            f"{len(report.unknown)} instance_id(s) not in {Path(path).name}, "
            f"e.g. {report.unknown[:3]}. Mining creates rows; stages only update them.")

    moved = [rid for rid, row in by_id.items() if _without(row, block) != before[rid]]
    if moved:
        raise StoreError(
            f"write to {block!r} also changed {len(moved)} row(s) outside that block, "
            f"e.g. {moved[:3]}. A stage may only write the block it owns.")

    if not dry_run and report.changed:
        save(by_id.values(), path)
    return report


def query(rows: Iterable[dict], **preds: Any) -> list[dict]:
    """Filter rows, e.g. judge__gradeable=True."""
    def get(row: dict, key: str) -> Any:
        cur: Any = row
        for part in key.split("__"):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return cur

    out = []
    for row in rows:
        if all(want(get(row, k)) if isinstance(want := v, Callable) else get(row, k) == v
               for k, v in preds.items()):
            out.append(row)
    return out


# ------------------------------------------------------------------ test lists

def load_test_lists(path: Path | str = TEST_LISTS) -> dict[str, dict]:
    """`{instance_id: test lists}`."""
    return {r["instance_id"]: r for r in load(path)}


def save_test_lists(lists: dict[str, dict], path: Path | str = TEST_LISTS,
                    *, snapshot: bool = False) -> int:
    """Replace the test-list table."""
    return save(lists.values(), path, snapshot=snapshot)
