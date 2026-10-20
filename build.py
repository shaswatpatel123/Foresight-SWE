"""Build website/index.html: inline dataset metadata (+ results, when present) into template.html.

    python3 build.py [--dataset path/to/verified.jsonl] [--template template.html --out index.html]

Without --dataset, the verified split is downloaded from the HF dataset once and cached in .cache/
(gitignored). The dataset is gated, so set HF_TOKEN or run `huggingface-cli login` first.

Patches and PASS_TO_PASS lists are left out; the page links to the HF dataset for those.
Leaderboard rows come from website/leaderboard.json (currently the draft paper's tables). Static assets
(owl, paper figures) live in website/assets/ and are referenced relatively, so the folder serves as-is.
"""
import argparse, json
from datetime import datetime
from pathlib import Path

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


HF_VERIFIED = "https://huggingface.co/datasets/shaswat123/foresightcoder-anticipation/resolve/main/data/verified.jsonl"


def default_dataset() -> Path:
    cached = HERE / ".cache" / "verified.jsonl"
    if not cached.exists():
        import os, urllib.error, urllib.request
        # the dataset is gated: send the HF token of an account with approved access
        tok_file = Path.home() / ".cache" / "huggingface" / "token"
        token = os.environ.get("HF_TOKEN") or (tok_file.read_text().strip() if tok_file.exists() else "")
        req = urllib.request.Request(HF_VERIFIED, headers={"Authorization": f"Bearer {token}"} if token else {})
        print(f"downloading {HF_VERIFIED} -> {cached.relative_to(HERE)}")
        try:
            data = urllib.request.urlopen(req).read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit("the HF dataset is gated: set HF_TOKEN (or run `huggingface-cli login`) "
                                 "with an account that has access, or pass --dataset") from None
            raise
        cached.parent.mkdir(exist_ok=True)
        cached.write_bytes(data)
    return cached


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", help="verified split jsonl (default: download from HF)")
    ap.add_argument("--template", default="template.html", help="template file under website/")
    ap.add_argument("--out", default="index.html", help="output file under website/")
    a = ap.parse_args()
    rows = [slim(json.loads(l)) for l in open(a.dataset or default_dataset())]
    board = json.loads((HERE / "leaderboard.json").read_text())
    data = {"instances": rows, "leaderboard": board}
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = (HERE / a.template).read_text().replace("/*__DATA__*/null", blob)
    (HERE / a.out).write_text(html)
    print(f"wrote {a.out} ({len(html)//1024} KB, {len(rows)} instances, {len(board['rows'])} leaderboard rows)")


if __name__ == "__main__":
    main()
