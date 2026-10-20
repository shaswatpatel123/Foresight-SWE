#!/usr/bin/env python3
"""Map the filter judge's verdict to keep/drop."""
from __future__ import annotations

_ELIG_TO_QUALITY = {
    "clean": "clean",
    "not_a_bug": "not_a_bug",
    "new_feature": "flag_newfeature",
    "unpredictable": "flag_unpredictable",
    "weak": "weak",
}


def derive_filter(eligibility: str, validity_code: str = "A") -> dict:
    quality = _ELIG_TO_QUALITY.get(eligibility, "unreviewed")
    task_valid = validity_code == "A"
    return {
        "quality": quality,
        "task_valid": task_valid,
        # real bug and valid task
        "gradeable": eligibility == "clean" and task_valid,
    }
