"""Export security chains in the judge's input shape."""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from foresight import config

SECDIR = ROOT / "data" / "chains" / "security"
SRC = SECDIR / "security_chains.jsonl"
POOL = SECDIR / "security_pool.jsonl"
S1A = SECDIR / "security_stage1a.jsonl"

rows = [json.loads(l) for l in SRC.open() if l.strip()]
MAX_TIER = int(sys.argv[1]) if len(sys.argv) > 1 else config.DOCKER_RECENCY_YEARS
pool, s1a, skipped, screened_out = [], [], [], []
for r in rows:
    try:
        config.get_target(r["repo"])
    except Exception:
        skipped.append(r["repo"])
        continue
    f = r.get("filters") or {}
    tier = f.get("recency_tier")
    # screens before judging
    screens = {
        "has_pr2_tests": bool(f.get("pr2_has_test_changes")),
        "has_pr2_source": bool(f.get("pr2_n_src_files")),
        "no_date_inversion": not r.get("date_inversion"),
        "in_recency_window": tier is not None and tier <= MAX_TIER,
    }
    if not all(screens.values()):
        screened_out.append((r["instance_id"],
                             [k for k, v in screens.items() if not v]))
        continue
    sec = r.get("security") or {}
    pool.append({
        "instance_id": r["instance_id"], "repo": r["repo"],
        "language": r["language"], "category": r.get("category"),
        "earlier_pr": r["introducer_pr"], "later_pr": r["fixer_pr"],
        "pr1_base_sha": r["pr1_base_sha"], "pr1_head_sha": r["pr1_head_sha"],
        "pr2_base_sha": r["pr2_base_sha"], "pr2_head_sha": r["pr2_head_sha"],
        "pr1_date": r["pr1_date"], "pr2_date": r["pr2_date"],
        "recency_tier": tier,
        "evidence": (r["sources"][0]["evidence"] if r.get("sources") else "")
                    + f"  [{sec.get('cve_id')} {','.join(sec.get('cwes') or [])}"
                      f" {sec.get('severity')}]",
    })
    s1a.append({"instance_id": r["instance_id"], "pr2_has_test_changes": True})

POOL.write_text("".join(json.dumps(r) + "\n" for r in pool))
S1A.write_text("".join(json.dumps(r) + "\n" for r in s1a))
import collections
print(f"{len(pool)} judge-ready rows -> {POOL}")
if skipped:
    c = collections.Counter(skipped)
    print(f"skipped {sum(c.values())} rows: repos not in config.TARGETS -> {dict(c)}")
if screened_out:
    why = collections.Counter(k for _, ks in screened_out for k in ks)
    print(f"screened out {len(screened_out)} (tier cap {MAX_TIER}): {dict(why)}")
noprs = sum(1 for r in pool if not r["earlier_pr"])
print(f"introducer_pr == 0 (commit-anchored, no PR text): {noprs}/{len(pool)}")
