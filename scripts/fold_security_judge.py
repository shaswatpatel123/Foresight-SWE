"""Write judge verdicts into security chain rows."""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from foresight import store  # noqa: E402

import argparse

_ap = argparse.ArgumentParser()
_ap.add_argument("--store", default=str(ROOT / "data" / "chains" / "security" / "security_chains.jsonl"),
                 help="chain file to write the judge block into (default: the "
                      "security side file; pass the master once merged)")
_ap.add_argument("--verdicts", default=str(ROOT / "data" / "security_judge.jsonl"))
_args = _ap.parse_args()
CHAINS = pathlib.Path(_args.store)
VERDICTS = pathlib.Path(_args.verdicts)

KEEP = ("eligibility", "validity", "impact", "reach", "detectability",
        "severity", "security", "mechanism", "rationale", "filter_reason")

updates = {}
for line in VERDICTS.open():
    if not line.strip():
        continue
    v = json.loads(line)
    if v.get("error") or not v.get("eligibility"):
        continue                      # unjudged: no block
    verdict = {k: v.get(k) for k in KEEP if k in v}
    verdict["model"] = "deepseek-v4-flash"
    updates[v["instance_id"]] = {
        "outcome": "ok",
        "verdicts": [verdict],
        "gradeable": bool((v.get("derived") or {}).get("gradeable")),
    }

report = store.update("judge", updates, path=CHAINS)
print(report)
print(f"folded {len(updates)} verdicts into {CHAINS.name}")
