"""Merge validated chains into one dataset."""

from __future__ import annotations

import glob
import json
from collections import Counter

from . import config


def load_validated() -> list[dict]:
    """All validated chains, deduped."""
    seen: dict[tuple[str, int, int], dict] = {}
    for path in sorted(glob.glob(str(config.CHAINS_DIR / "*_validated*.jsonl"))):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("status") != "validated":
                continue
            key = (row["repo"], row["earlier_pr"], row["later_pr"])
            prev = seen.get(key)
            # prefer the copy with PR1 tests
            if prev is None or (not prev.get("pr1_f2p_tests") and row.get("pr1_f2p_tests")):
                seen[key] = row
    return list(seen.values())


_VERDICTS_PATH = config.DATA_DIR / "quality_verdicts.json"
_JUDGES_PATH = config.DATA_DIR / "judge_results.json"
_MUTATION_PATH = config.DATA_DIR / "mutation_results.json"

# judge label -> quality
_JUDGE_TO_QUALITY = {
    "missed_edge_case": "clean",
    "new_feature": "flag_newfeature",
    "regression_unrelated": "flag_regression",
    "test_infra": "borderline_meta",
    "weak": "weak",
}


def apply_quality(rows: list[dict]) -> list[dict]:
    """Add quality labels to rows."""
    verdicts = json.loads(_VERDICTS_PATH.read_text()) if _VERDICTS_PATH.exists() else {}
    judges = json.loads(_JUDGES_PATH.read_text()) if _JUDGES_PATH.exists() else {}
    muts = {}
    if _MUTATION_PATH.exists():
        for m in json.loads(_MUTATION_PATH.read_text()):
            muts[(m["repo"], m["later_pr"])] = m["mutation_score"]

    for r in rows:
        key = f"{r['repo']}/PR{r['earlier_pr']}->PR{r['later_pr']}"
        mkey = f"{r['repo']}/{r['earlier_pr']}->{r['later_pr']}"  # verdicts key
        if mkey in verdicts:
            r["quality_manual"], r["quality_reason"] = verdicts[mkey]
        judged = judges.get(key)
        if judged:
            r["judge_labels"] = judged["labels"]
            r["judge_consensus"] = judged["consensus"]
            r["quality"] = _JUDGE_TO_QUALITY.get(judged["consensus"], "unreviewed")
        else:
            r.setdefault("quality", r.get("quality_manual", "unreviewed"))
        r["mutation_score"] = muts.get((r["repo"], r["later_pr"]))
    return rows


def stats(rows: list[dict]) -> dict:
    eval_ready = [r for r in rows if r.get("pr1_f2p_tests")]
    clean = [r for r in rows if r.get("quality") == "clean"]
    clean_muts = [r["mutation_score"] for r in clean if r.get("mutation_score") is not None]
    avg_mut = round(sum(clean_muts) / len(clean_muts), 2) if clean_muts else None
    return {
        "validated_chains": len(rows),
        "clean_chains": len(clean),  # missed edge cases
        "repos_clean": sorted({r["repo"] for r in clean}),
        "n_repos": len({r["repo"] for r in rows}),
        "by_repo": dict(Counter(r["repo"] for r in rows)),
        "by_quality": dict(Counter(r.get("quality", "unreviewed") for r in rows)),
        "clean_by_repo": dict(Counter(r["repo"] for r in clean)),
        "total_anticipation_tests": sum(len(r["anticipation_tests"]) for r in rows),
        "clean_anticipation_tests": sum(len(r["anticipation_tests"]) for r in clean),
        "eval_ready_chains": len(eval_ready),
        "avg_mutation_score_clean": avg_mut,
        "manual_vs_judge_agreement": sum(
            1 for r in rows
            if r.get("quality_manual") == r.get("quality")) if rows else 0,
    }


def write_dataset(rows: list[dict]) -> str:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = config.DATA_DIR / "dataset.jsonl"
    with open(out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(out)
