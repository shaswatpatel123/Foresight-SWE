#!/usr/bin/env python3
"""Shared judge helpers; also writes PR1 statements."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import time
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "judges" / "prompts" / "swe_rebench_v2"
BUNDLE = ROOT / "data" / "judge_poc.jsonl"
OUT = ROOT / "data" / "judge_poc_results.jsonl"

# any OpenAI-compatible server; keep reasoning on
API_URL = os.environ.get("DEEPSEEK_BASE_URL",
                         "https://api.deepseek.com").rstrip("/") + "/chat/completions"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
EXTRA_BODY: dict = json.loads(os.environ.get("DEEPSEEK_EXTRA_BODY", "{}"))
PRICE_IN, PRICE_OUT = 0.14 / 1e6, 0.28 / 1e6  # USD per token, v4-flash

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render(template: str, **vars: str) -> str:
    return _VAR.sub(lambda m: str(vars.get(m.group(1), "")), template)


def load_template(name: str) -> str:
    return (PROMPTS / name).read_text()


_EX = re.compile(r"\{\{\s*examples\[(\d)\]\.problem_statement\s*-?\}\}")


def render_clarity(template: str, *, repo: str, problem_statement: str,
                   anchors: list[str]) -> str:
    """Render verified.j2 with its examples."""
    t = _EX.sub(lambda m: anchors[int(m.group(1))], template)
    return render(t, repo=repo, problem_statement=problem_statement)


def parse_score(text: str) -> int | None:
    """Last 0-3 in the reply."""
    nums = re.findall(r"\b([0-3])\b", text)
    return int(nums[-1]) if nums else None


_USAGE = {"in": 0, "out": 0, "calls": 0, "truncated": 0}
# max tokens after length retries
_MAX_TOKENS_CEILING = 128000


class JudgeAPIError(RuntimeError):
    """Reply had no completion."""


def reasoning_chars(message: dict) -> int:
    """Length of the reasoning trace."""
    r = message.get("reasoning") or message.get("reasoning_content") or ""
    if not r and "<think" in (message.get("content") or "").lower():
        return len(message.get("content") or "")
    return len(r)


def probe_reasoning(timeout: float = 180.0) -> tuple[int, int]:
    """Does the endpoint reason? (chars, tokens)."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user",
                      "content": "Reason briefly, then reply with only: OK"}],
        "max_tokens": 2000,
        "temperature": 0,
        **EXTRA_BODY,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if "choices" not in d:
        raise JudgeAPIError(f"preflight got no choices: {str(d)[:200]}")
    msg = d["choices"][0].get("message") or {}
    return reasoning_chars(msg), (d.get("usage") or {}).get("completion_tokens", 0)


def deepseek(prompt: str, max_tokens: int = 64000, attempts: int = 5) -> str:
    """Call the judge, retrying network errors."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        **EXTRA_BODY,
        "stream": False,
    }).encode()
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                API_URL, data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}",
                },
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read())
            if "choices" not in d:
                # show the API's message
                msg = (d.get("error") or {}).get("message") or str(d)[:200]
                raise JudgeAPIError(f"no choices in response: {msg}")
            u = d.get("usage", {})
            _USAGE["in"] += u.get("prompt_tokens", 0)
            _USAGE["out"] += u.get("completion_tokens", 0)
            _USAGE["calls"] += 1
            # count calls that reasoned
            if reasoning_chars(d["choices"][0].get("message") or {}):
                _USAGE["reasoned"] = _USAGE.get("reasoned", 0) + 1
            # cut off: retry with double budget
            fr = (d["choices"][0] or {}).get("finish_reason")
            if fr == "length":
                _USAGE["truncated"] = _USAGE.get("truncated", 0) + 1
                if max_tokens < _MAX_TOKENS_CEILING:
                    return deepseek(prompt, min(max_tokens * 2, _MAX_TOKENS_CEILING),
                                    attempts=attempts)
            return d["choices"][0]["message"]["content"] or ""
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError,
                socket.timeout, json.JSONDecodeError, JudgeAPIError) as e:
            last = e
            if attempt == attempts - 1:
                break
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"deepseek failed after {attempts} attempts: "
                       f"{type(last).__name__}: {last}")


def extract_json(text: str) -> dict | None:
    """First JSON object in a reply."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


def cap(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n... [truncated {len(s) - n} chars]"


def judge_chain(rec: dict, templates: dict, passes: list[str], capn: int,
                max_tokens: int = 32000) -> dict:
    repo = rec["repo"]
    out = {
        "instance_id": rec["instance_id"],
        "repo": repo,
        "earlier_pr": rec["earlier_pr"],
        "later_pr": rec["later_pr"],
        "prior_quality": rec.get("prior_quality", ""),
        "prior_judge_consensus": rec.get("prior_judge_consensus", ""),
    }
    try:
        if "problem_statement" in passes:
            p = render(templates["pr_description"], repo=repo,
                       pr_description=rec["pr1_problem_statement"],
                       patch=cap(rec["pr1_patch"] or rec["pr1_test_patch"], capn))
            out["generated_problem_statement"] = deepseek(p, max_tokens)

        interface = ""
        if "interfaces" in passes or "meta" in passes:
            p = render(templates["interfaces"], repo=repo,
                       problem_statement=rec["pr2_problem_statement"],
                       test_patch=cap(rec["pr2_test_patch"], capn),
                       patch=cap(rec["pr2_patch"], capn))
            interface = deepseek(p, max_tokens)
            out["interface"] = interface

        if "meta" in passes:
            p = render(templates["meta_info"], repo=repo,
                       problem_statement=rec["pr2_problem_statement"],
                       interface=interface,
                       test_patch=cap(rec["pr2_test_patch"], capn),
                       patch=cap(rec["pr2_patch"], capn))
            raw = deepseek(p, max_tokens)
            meta = extract_json(raw)
            out["meta"] = meta
            out["meta_raw"] = raw if meta is None else ""

        if "taskcode" in passes:
            # A/B code for the PR1 task
            p = render(templates["interfaces"], repo=repo,
                       problem_statement=rec["pr1_problem_statement"],
                       test_patch=cap(rec["pr1_test_patch"], capn),
                       patch=cap(rec["pr1_patch"], capn))
            iface1 = deepseek(p, max_tokens)
            p = render(templates["meta_info"], repo=repo,
                       problem_statement=rec["pr1_problem_statement"],
                       interface=iface1,
                       test_patch=cap(rec["pr1_test_patch"], capn),
                       patch=cap(rec["pr1_patch"], capn))
            raw = deepseek(p, max_tokens)
            m = extract_json(raw)
            out["pr1_meta"] = m
            out["pr1_meta_raw"] = raw if m is None else ""

        if "clarity" in passes:
            # score what the agent sees
            stmt = rec.get("generated_problem_statement") or rec["pr1_problem_statement"]
            p = render_clarity(templates["verified"], repo=repo,
                               problem_statement=stmt, anchors=templates["anchors"])
            raw = deepseek(p, max_tokens)
            out["clarity_raw"] = raw
            out["clarity_score"] = parse_score(raw)
    except Exception as e:  # record, keep going
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# prior consensus label -> coarse bucket
_PRIOR_BUCKET = {
    "missed_edge_case": "bug",
    "new_feature": "feat",
    "test_infra": "infra",
    "weak": "weak",
}
_BUG_CATS = {"critical_bug", "major_bug", "minor_bug", "regression_bug",
             "edge_case_bug", "performance_bug", "security_bug"}
_FEAT_CATS = {"integration_feat", "core_feat", "ui_ux_feat"}
_ENH_CATS = {"dev_ops_enh", "documentation_enh"}


def meta_bucket(meta: dict | None) -> str:
    if not meta:
        return "?"
    cats = set(meta.get("pr_categories", []) or [])
    if cats & _BUG_CATS:
        return "bug"
    if cats & _FEAT_CATS:
        return "feat"
    if cats & _ENH_CATS:
        return "infra"
    return "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", default="problem_statement,interfaces,meta")
    ap.add_argument("--only-labeled", action="store_true",
                    help="only chains that already have a prior consensus label")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--cap", type=int, default=30000)
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--bundle", default=str(BUNDLE))
    args = ap.parse_args()
    passes = [p.strip() for p in args.passes.split(",") if p.strip()]

    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("DEEPSEEK_API_KEY not set")

    templates = {n: load_template(f"{n}.j2") for n in
                 ("pr_description", "interfaces", "meta_info")}
    if "clarity" in passes:
        templates["verified"] = load_template("verified.j2")
        anchors_doc = json.loads((PROMPTS / "clarity_anchors.json").read_text())
        templates["anchors"] = [e["problem_statement"] for e in anchors_doc["examples"]]

    recs = [json.loads(l) for l in Path(args.bundle).read_text().splitlines() if l.strip()]

    # clarity: reuse generated statements
    if "clarity" in passes and Path(args.out).exists():
        prior = {json.loads(l)["instance_id"]: json.loads(l)
                 for l in Path(args.out).read_text().splitlines() if l.strip()}
        for r in recs:
            g = prior.get(r["instance_id"], {}).get("generated_problem_statement")
            if g:
                r["generated_problem_statement"] = g

    if args.only_labeled:
        recs = [r for r in recs if r.get("prior_judge_consensus")]
    if args.limit:
        recs = recs[:args.limit]

    print(f"model={MODEL}  passes={passes}  chains={len(recs)}  workers={args.workers}")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(judge_chain, r, templates, passes, args.cap, args.max_tokens): r for r in recs}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            results.append(res)
            if res.get("error"):
                tag = "ERR"
            elif "clarity" in passes and "meta" not in passes:
                tag = f"clarity={res.get('clarity_score')}"
            else:
                tag = meta_bucket(res.get("meta"))
            print(f"  [{i}/{len(recs)}] {res['instance_id']:32} -> {tag}"
                  f"  (prior={res.get('prior_quality') or '-'})")

    # merge with earlier results
    merged = {}
    if Path(args.out).exists():
        for l in Path(args.out).read_text().splitlines():
            if l.strip():
                d = json.loads(l)
                merged[d["instance_id"]] = d
    for r in results:
        merged.setdefault(r["instance_id"], {}).update(r)
    out_rows = sorted(merged.values(), key=lambda r: r["instance_id"])
    Path(args.out).write_text("\n".join(json.dumps(r) for r in out_rows) + "\n")

    # ---- cost ----
    cost = _USAGE["in"] * PRICE_IN + _USAGE["out"] * PRICE_OUT
    print(f"\nusage: {_USAGE['calls']} calls | in={_USAGE['in']:,} out={_USAGE['out']:,}"
          f" | est cost ${cost:.4f}")

    # ---- agreement on labeled chains ----
    if "meta" in passes:
        labeled = [r for r in results if r.get("prior_judge_consensus") and r.get("meta")]
        if labeled:
            agree = 0
            print(f"\nagreement on {len(labeled)} prior-labeled chains "
                  f"(prior_bucket vs deepseek_bucket):")
            for r in labeled:
                pb = _PRIOR_BUCKET.get(r["prior_judge_consensus"], "?")
                db = meta_bucket(r["meta"])
                ok = (pb == db)
                agree += ok
                print(f"  {'OK ' if ok else 'XX '} {r['instance_id']:32}"
                      f" prior={pb:5} deepseek={db:5}"
                      f" cats={r['meta'].get('pr_categories')}")
            print(f"\nbucket agreement: {agree}/{len(labeled)} = {agree/len(labeled):.0%}")

    # ---- clarity distribution ----
    if "clarity" in passes:
        from collections import Counter
        scored = [r for r in results if r.get("clarity_score") is not None]
        dist = Counter(r["clarity_score"] for r in scored)
        ready = sum(dist.get(s, 0) for s in (0, 1))
        print(f"\nclarity (PR1 task, 0=clear .. 3=unworkable) over {len(scored)}:")
        for s in (0, 1, 2, 3):
            print(f"  {s}: {dist.get(s, 0)}")
        print(f"  eval-ready (score 0-1): {ready}/{len(scored)} = "
              f"{ready/len(scored):.0%}" if scored else "  (none scored)")
        vague = [r["instance_id"] for r in scored if r["clarity_score"] in (2, 3)]
        if vague:
            print(f"  vague/unworkable (2-3): {vague}")

    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
