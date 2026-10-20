"""Why a chain cannot reach the next stage, checked in pipeline order (earliest gate wins)."""
from __future__ import annotations

from . import config

#: languages the docker planner can build images for
RUNNABLE = ("python", "javascript")


def gate_reason(row: dict, max_tier: int = config.DOCKER_RECENCY_YEARS) -> str | None:
    """Why this chain cannot proceed, or None if nothing stops it."""
    if row.get("excluded"):
        return "excluded"
    judge = row.get("judge") or {}
    if not judge:
        return "not_judged"
    if not judge.get("gradeable"):
        return "not_gradeable"
    f = row.get("filters") or {}
    tier = f.get("recency_tier")
    if tier is None or tier > max_tier:
        return "tier_above_max"
    if f.get("pr2_has_test_changes") is False:
        return "no_pr2_test_changes"
    if f.get("pr1_has_test_changes") is False:
        return "anticipation_only"
    if row.get("language") not in RUNNABLE:
        return "no_runner"
    return None
