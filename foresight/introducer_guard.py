"""Drop chains whose introducer post-dates the bug report."""
from __future__ import annotations

import json
from pathlib import Path

from . import config

AUDIT_GLOB = "*_introducer_audit.json"


def load_flags(chains_dir: Path | None = None) -> dict[str, dict]:
    """Flagged chains, by instance_id."""
    d = chains_dir or config.CHAINS_DIR
    flags: dict[str, dict] = {}
    for p in sorted(Path(d).glob(AUDIT_GLOB)):
        try:
            rows = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(rows, list):
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("impossible") and r.get("instance_id"):
                flags[r["instance_id"]] = {**r, "audit_file": p.name}
    return flags


def audited_ids(chains_dir: Path | None = None) -> set[str]:
    """All audited instances, flagged or not."""
    d = chains_dir or config.CHAINS_DIR
    seen: set[str] = set()
    for p in sorted(Path(d).glob(AUDIT_GLOB)):
        try:
            rows = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(rows, list):
            seen.update(r["instance_id"] for r in rows
                        if isinstance(r, dict) and r.get("instance_id"))
    return seen
