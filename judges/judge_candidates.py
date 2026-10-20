#!/usr/bin/env python3
"""Filter and severity judge for candidate chains."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "judges"))

import render_judge_prompts as rjp
from filter_map import derive_filter
from severity_map import derive_severity
from foresight import config, secrets
from foresight.mining import github_api
from foresight.langspec import get_spec

POOL = ROOT / "data" / "chains" / "candidate_pool.jsonl"
OUT = ROOT / "data" / "pool_judge.jsonl"
FILTER_PROMPT = (Path(__file__).resolve().parent / "prompts" / "filter.j2").read_text()
SEV_PROMPT = (Path(__file__).resolve().parent / "prompts" / "severity.j2").read_text()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, errors="replace").stdout


def diff_split(repo: Path, base: str, head: str, spec) -> tuple[str, str]:
    names = [n for n in _git(repo, "diff", "--name-only", base, head).splitlines() if n]
    src = [n for n in names if spec.is_source(n)]
    tst = [n for n in names if spec.is_test_path(n)]
    sp = _git(repo, "diff", base, head, "--", *src) if src else ""
    tp = _git(repo, "diff", base, head, "--", *tst) if tst else ""
    return sp, tp


def commit_msg(repo: Path, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%s%n%n%b", sha).strip()


def pr_text(client, slug, num, repo, sha, is_django) -> tuple[str, str]:
    """(title, body); commit message for django."""
    if is_django or not client:
        return "", commit_msg(repo, sha)
    d = client.get(f"/repos/{slug}/issues/{num}", allow_404=True) or {}
    if not d:
        return "", commit_msg(repo, sha)
    return d.get("title") or "", d.get("body") or ""


def judge_one(row, do_sev, clients) -> dict:
    iid, repo = row["instance_id"], row["repo"]
    out = {"instance_id": iid, "repo": repo, "language": row.get("language"),
           "category": row.get("category")}
    try:
        t = config.get_target(repo)
        rp = t.path
        spec = get_spec(t.language)
        is_dj = t.runner == "django"
        client = clients.get(repo)
        fix_patch, test_patch = diff_split(rp, row["pr2_base_sha"], row["pr2_head_sha"], spec)
        if not test_patch.strip():
            out["skip"] = "no_test_changes"
            return out
        p1t, p1b = pr_text(client, t.slug, row["earlier_pr"], rp, row["pr1_head_sha"], is_dj)
        p2t, p2b = pr_text(client, t.slug, row["later_pr"], rp, row["pr2_head_sha"], is_dj)
        disc = f"[PR2/fixer #{row['later_pr']}] {p2t}\n{p2b[:1500]}\n[PR1 #{row['earlier_pr']}] {p1t}\n{p1b[:1500]}"
        ev = (f"category={row.get('category')}  evidence={row.get('evidence')}  "
              f"pr1_date={row.get('pr1_date')}  pr2_date={row.get('pr2_date')}")
        common = dict(repo=repo, pr1_task=rjp.cap(f"{p1t}\n{p1b}", 4000),
                      pr2_description=rjp.cap(f"{p2t}\n{p2b}", 4000),
                      fix_patch=rjp.cap(fix_patch, 20000), test_patch=rjp.cap(test_patch, 20000),
                      discussion=rjp.cap(disc, 12000), evidence=ev)
        fv = rjp.extract_json(rjp.deepseek(rjp.render(FILTER_PROMPT, **common))) or {}
        elig = (fv.get("eligibility") or {}).get("value")
        code = (fv.get("validity") or {}).get("code")
        out["eligibility"], out["validity"] = elig, code
        out["derived"] = derive_filter(elig, code or "A")
        out["filter_reason"] = fv.get("reasoning", "")
        if do_sev and out["derived"]["gradeable"]:
            sv = rjp.extract_json(rjp.deepseek(rjp.render(SEV_PROMPT, **common))) or {}
            imp = (sv.get("impact") or {}).get("value")
            reach = (sv.get("reach") or {}).get("value")
            det = (sv.get("detectability") or {}).get("value")
            out["impact"], out["reach"], out["detectability"] = imp, reach, det
            out["severity"] = derive_severity(imp, reach, det)
            out["security"] = bool((sv.get("security") or {}).get("is_security"))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def stratified(rows: list[dict]) -> list[dict]:
    """Round-robin across repos."""
    by = defaultdict(list)
    for r in rows:
        by[r["repo"]].append(r)
    order, i = [], 0
    while True:
        added = False
        for repo in sorted(by):
            if i < len(by[repo]):
                order.append(by[repo][i]); added = True
        if not added:
            break
        i += 1
    return order


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100, help="batch size (0 = all remaining)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--severity", action="store_true")
    ap.add_argument("--retry-errors", action="store_true",
                    help="drop rows that carry no verdict -- a transport error, a dead "
                         "endpoint, or a reply whose JSON did not parse -- and judge "
                         "them again; without this they occupy the resume set forever")
    ap.add_argument("--allow-nonreasoning", action="store_true",
                    help="judge even if the endpoint returns no reasoning trace "
                         "(deliberate ablation only -- verdicts will not be "
                         "comparable with the rest of the pool)")
    ap.add_argument("--repos", default="", help="restrict to these repos")
    ap.add_argument("--lang", default="",
                    help="restrict to these languages (comma list, e.g. "
                         "'javascript,typescript'). Selects by the pool row's "
                         "`language` field, so a whole language stratum can be judged "
                         "without naming every repo in it.")
    ap.add_argument("--max-recency-tier", type=int, default=0,
                    help="only judge chains with recency_tier <= N (0 = no limit). "
                         "N=5 is the Docker-built subset; judging beyond it spends "
                         "tokens on chains no image can currently run.")
    # one output file per run
    ap.add_argument("--pool", default=str(POOL), help="candidate pool to judge")
    ap.add_argument("--out", default=str(OUT), help="verdict jsonl (appended, resumable)")
    ap.add_argument("--stage1a", default="", help="stage-1a screen gating this pool "
                                                  "(default: data/chains/pool_stage1a.jsonl)")
    args = ap.parse_args()
    pool_path = Path(args.pool)
    out_path = Path(args.out)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        tok = ROOT / ".deepseek_token"
        if not tok.exists():
            sys.exit("no .deepseek_token")
        os.environ["DEEPSEEK_API_KEY"] = tok.read_text().strip()
    token = secrets.github_token()

    # the judge must reason
    rc, ct = rjp.probe_reasoning()
    print(f"preflight: {rjp.API_URL} model={rjp.MODEL} "
          f"reasoning={rc} chars, completion_tokens={ct}"
          + (f" extra_body={rjp.EXTRA_BODY}" if rjp.EXTRA_BODY else ""), flush=True)
    if not rc and not args.allow_nonreasoning:
        sys.exit(
            "REFUSING TO JUDGE: this endpoint returned no reasoning trace, so its\n"
            "verdicts would not be comparable with the rows judged so far.\n"
            "For a vLLM server, enable the model's thinking pass:\n"
            "  DEEPSEEK_EXTRA_BODY='{\"chat_template_kwargs\": {\"thinking\": true}}'\n"
            "Unknown/mis-spelled keys are accepted silently, so re-run and confirm\n"
            "the preflight line reports a non-zero reasoning length.\n"
            "Pass --allow-nonreasoning only for a deliberate ablation.")

    rows = [json.loads(l) for l in pool_path.read_text().splitlines() if l.strip()]
    if args.repos:
        want = {r.strip() for r in args.repos.split(",")}
        rows = [r for r in rows if r["repo"] in want]
    if args.lang:
        langs = {l.strip() for l in args.lang.split(",") if l.strip()}
        before = len(rows)
        rows = [r for r in rows if r.get("language") in langs]
        print(f"language: {len(rows)}/{before} are {sorted(langs)} "
              f"({before - len(rows)} deferred, NOT dropped)", flush=True)
    if args.max_recency_tier:
        before = len(rows)
        rows = [r for r in rows
                if r.get("recency_tier") is not None
                and r["recency_tier"] <= args.max_recency_tier]
        print(f"recency: {len(rows)}/{before} have recency_tier <= "
              f"{args.max_recency_tier} ({before - len(rows)} deferred, NOT dropped "
              f"-- re-run with a higher -N to judge them)", flush=True)
    # only chains with test changes
    s1a = Path(args.stage1a) if args.stage1a else ROOT / "data" / "chains" / "pool_stage1a.jsonl"
    if s1a.exists():
        keep = set()
        for l in s1a.read_text().splitlines():
            if l.strip():
                d = json.loads(l)
                if d.get("pr2_has_test_changes", d.get("has_test_changes")):
                    keep.add(d["instance_id"])
        before = len(rows)
        rows = [r for r in rows if r["instance_id"] in keep]
        print(f"stage-1a: {len(rows)}/{before} have test changes (LLM-judging those)", flush=True)
    rows = stratified(rows)
    # rows without a verdict get re-judged
    done = set()
    if out_path.exists():
        # `prev`, not `rows`
        prev = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
        # done = verdict or skip reason
        def incomplete(r: dict) -> bool:
            return bool(r.get("error")) or not (r.get("eligibility") or r.get("skip"))

        errored = [r for r in prev if incomplete(r)]
        if errored and args.retry_errors:
            keep = [r for r in prev if not incomplete(r)]
            shutil.copy2(out_path, out_path.with_suffix(out_path.suffix + ".bak"))
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in keep))
            tmp.replace(out_path)
            print(f"retry-errors: dropped {len(errored)} incomplete rows "
                  f"({len(keep)} verdicts kept, .bak written) -- they re-judge now",
                  flush=True)
            prev = keep
        elif errored:
            print(f"NOTE: {len(errored)} rows are incomplete (error or no verdict) "
                  f"and will be SKIPPED; pass --retry-errors to re-judge them",
                  flush=True)
        done = {r["instance_id"] for r in prev}
    todo = [r for r in rows if r["instance_id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    clients = {r["repo"]: github_api.GitHubClient(token, r["repo"]) for r in todo} if token else {}
    print(f"{len(done)} already judged; this batch: {len(todo)} (severity={args.severity})", flush=True)

    with out_path.open("a") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(judge_one, r, args.severity, clients) for r in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            o = fut.result()
            f.write(json.dumps(o) + "\n"); f.flush()
            tag = o.get("eligibility") or o.get("skip") or o.get("error", "?")
            sev = f" {o.get('severity')}{'/SEC' if o.get('security') else ''}" if o.get("severity") else ""
            print(f"  [{i}/{len(todo)}] {o['instance_id']:28s} {tag}{sev}", flush=True)

    report(out_path)


def report(out_path: Path = OUT) -> None:
    res = [json.loads(l) for l in Path(out_path).read_text().splitlines() if l.strip()]
    judged = [r for r in res if "eligibility" in r]
    skip = sum(1 for r in res if r.get("skip"))
    err = sum(1 for r in res if r.get("error"))
    print(f"\n===== cumulative: {len(res)} rows | {len(judged)} judged, "
          f"{skip} no_test_changes, {err} errors =====")
    if judged:
        elig = Counter(r["eligibility"] for r in judged)
        g = [r for r in judged if r["derived"]["gradeable"]]
        sev = Counter(r.get("severity") for r in g if r.get("severity"))
        sec = sum(1 for r in g if r.get("security"))
        print(f"eligibility: {dict(elig)}")
        print(f"GRADEABLE (clean+A): {len(g)}/{len(judged)} = {len(g)/len(judged)*100:.0f}%")
        if sev:
            print(f"severity of gradeable: {dict(sev)} | SECURITY: {sec}")


if __name__ == "__main__":
    main()
