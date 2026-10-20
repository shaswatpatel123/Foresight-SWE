"""Build website/index.html: inline dataset metadata (+ results, when present) into template.html.

    uv run python website/build.py [--dataset data/hf/data/verified.jsonl] [--template template.html --out index.html]

Patches and PASS_TO_PASS lists are left out; the page links to the HF dataset for those.
Leaderboard rows come from website/leaderboard.json (currently the draft paper's tables). Static assets
(owl, paper figures) live in website/assets/ and are referenced relatively, so the folder serves as-is.
"""
import argparse, json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def _list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v or []


def _date(s):
    return (s or "")[:10]


def slim(r):
    days = None
    if r.get("created_at") and r.get("pr2_created_at"):
        a = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(r["pr2_created_at"].replace("Z", "+00:00"))
        days = (b - a).days
    return {
        "id": r["instance_id"], "repo": r["repo"], "lang": r["language"],
        "pr1": r["earlier_pr"], "pr2": r["later_pr"],
        "pr1_date": _date(r.get("created_at")), "pr2_date": _date(r.get("pr2_created_at")), "days": days,
        "severity": r.get("severity"), "impact": r.get("impact"), "reach": r.get("reach"),
        "security": bool(r.get("security")), "source": r.get("introducer_found_by"),
        "task_type": r.get("pr1_task_type"), "failure_mode": r.get("failure_mode"),
        "pr1_difficulty": r.get("pr1_difficulty"), "pr2_difficulty": r.get("difficulty"), "runner": r.get("runner"),
        "statement": (r.get("problem_statement") or "")[:6000],
        "pr2_statement": (r.get("pr2_problem_statement") or "")[:3000],
        
        "n_f2p": len(_list(r.get("FAIL_TO_PASS"))), "n_ant": len(_list(r.get("ANTICIPATION_FAIL_TO_PASS"))),
        "n_p2p": len(_list(r.get("PASS_TO_PASS"))),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "data/hf/data/verified.jsonl"))
    ap.add_argument("--template", default="template.html", help="template file under website/")
    ap.add_argument("--out", default="index.html", help="output file under website/")
    a = ap.parse_args()
    rows = [slim(json.loads(l)) for l in open(a.dataset)]
    board = json.loads((HERE / "leaderboard.json").read_text())
    data = {"instances": rows, "leaderboard": board}
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = (HERE / a.template).read_text().replace("/*__DATA__*/null", blob)
    (HERE / a.out).write_text(html)
    print(f"wrote website/{a.out} ({len(html)//1024} KB, {len(rows)} instances, {len(board['rows'])} leaderboard rows)")


if __name__ == "__main__":
    main()
