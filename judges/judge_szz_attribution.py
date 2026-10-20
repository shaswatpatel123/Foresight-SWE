#!/usr/bin/env python3
"""Judge whether SZZ picked the right PR1; keep code A."""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "judges"))
import render_judge_prompts as rjp          # shared judge helpers
from foresight import config, secrets, store
from foresight.langspec import get_spec
from foresight.mining import github_api

PROMPT = (ROOT / "judges" / "prompts" / "szz_attribution.j2").read_text()
MASTER = ROOT / "data" / "chains_master.jsonl"
OUT = ROOT / "data" / "szz_attribution_judge.jsonl"
CODES = ("A", "C1", "C2", "C3")

# per-field char limits; must fit context
CAPS = dict(pr1_task=4000, pr2_description=4000, pr1_diff=30000, blamed_lines=8000,
            fix_patch=20000, test_patch=20000, discussion=12000, intervening=4000)
MAX_FILES, MAX_LINES, MAX_INTERVENING = 10, 60, 25
HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? ")

# forces a parseable JSON reply
SCHEMA = {"type": "object", "properties": {
    "defect": {"type": "string"}, "pr1_role": {"type": "string"},
    "reasoning": {"type": "string"}, "later_change": {"type": "string"},
    "code": {"type": "string", "enum": list(CODES)}, "evidence": {"type": "string"}},
    "required": ["defect", "pr1_role", "reasoning", "later_change", "code", "evidence"],
    "additionalProperties": False}

# answer cap, leaves room for prompt
rjp._MAX_TOKENS_CEILING = 80000


def git(repo: Path, *a: str, timeout: int = 120) -> tuple[bool, str]:
    """(ok, stdout); keeps errors distinct."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                           text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, ""
    return p.returncode == 0, p.stdout


def files_of(patch: str) -> set[str]:
    return set(re.findall(r"^diff --git a/(\S+)", patch or "", re.M))


def deleted_lines(repo, base, head, path, spec):
    """(line, text) PR2 deleted."""
    ok, out = git(repo, "diff", "--unified=0", base, head, "--", path)
    if not ok:
        return []
    ln, hits = 0, []
    for d in out.splitlines():
        m = HUNK.match(d)
        if m:
            ln = int(m.group(1)); continue
        if d.startswith(("---", "+++")):
            continue
        if d.startswith("-"):
            if spec.is_blamable(d[1:]) and ln >= 1:
                hits.append((ln, d[1:]))
            ln += 1
    return hits


def evidence(r, client):
    """Prompt inputs, or (None, reason)."""
    t = config.get_target(r["repo"])
    spec = get_spec(t.language)
    b1, h1 = r.get("pr1_base_sha"), r.get("pr1_head_sha")
    b2, h2 = r.get("pr2_base_sha"), r.get("pr2_head_sha")
    if not all((b1, h1, b2, h2)):
        return None, "missing_shas"

    ok1, pr1_all = git(t.path, "diff", b1, h1)
    ok2, fix_all = git(t.path, "diff", b2, h2)
    if not (ok1 and ok2):
        return None, "git_failed"
    p1 = {f for f in files_of(pr1_all) if spec.is_source(f)}
    p2 = {f for f in files_of(fix_all) if spec.is_source(f)}
    shared = sorted(p1 & p2)
    if not shared:
        # no shared file: reject
        return None, "file_disjoint"

    ok, rev = git(t.path, "rev-list", f"{b1}..{h1}")
    pr1_commits = set(rev.split()) if ok else set()

    blamed = []
    for path in shared[:MAX_FILES]:
        tgt = deleted_lines(t.path, b2, h2, path, spec)
        if not tgt:
            continue
        a = ["blame", "--porcelain"]
        for ln, _ in tgt[:MAX_LINES]:
            a += ["-L", f"{ln},{ln}"]
        ok, bl = git(t.path, *a, b2, "--", path)
        if not ok:
            continue
        shas = [l.split()[0] for l in bl.splitlines() if re.match(r"^[0-9a-f]{40} ", l)]
        for i, (ln, tx) in enumerate(tgt[:MAX_LINES]):
            if i < len(shas) and shas[i] in pr1_commits:
                blamed.append((path, ln, tx[:300]))

    # PR1 diff, shared files only
    keep, cur = [], None
    for line in pr1_all.splitlines(keepends=True):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/", line)
            cur = m.group(1) if m else None
        if cur in p2:
            keep.append(line)
    scoped = "".join(keep)

    ok, iv = git(t.path, "log", "--format=%s", f"{h1}..{b2}", "--",
                 *sorted({p for p, _, _ in blamed}) or shared[:MAX_FILES])
    subs = [l.strip() for l in (iv or "").splitlines() if l.strip()] if ok else []

    # split fix into source and tests
    fix = "".join(c for c in _by_file(fix_all)
                  if spec.is_source(_path_of(c)) and not spec.is_test_path(_path_of(c)))
    test = "".join(c for c in _by_file(fix_all) if spec.is_test_path(_path_of(c)))

    prose = _prose(r, client)
    return dict(
        repo=r["repo"],
        pr1_task=f"{prose['pr1_title']}\n{prose['pr1_body']}",
        pr1_diff=scoped or pr1_all,
        pr1_scope_note=(f"PR1 changed {len(p1)} source file(s); showing the "
                        f"{len(shared)} that PR2 also touched."
                        if scoped and len(shared) < len(p1)
                        else f"PR1 changed {len(p1)} source file(s); all shown."),
        blamed_lines="\n".join(f"{p}:{n}\n    {tx}" for p, n, tx in blamed)
                     or "(none isolated within the blame budget; judge from the diffs)",
        intervening_n=str(len(subs)),
        intervening="\n".join(f"  - {s}" for s in subs[:MAX_INTERVENING])
                    or "(none - no commit touched these files between PR1 and PR2)",
        pr2_description=f"{prose['pr2_title']}\n{prose['pr2_body']}",
        fix_patch=fix, test_patch=test, discussion=prose["discussion"] or "(none)",
    ), None


def _by_file(patch):
    out, cur = [], []
    for line in (patch or "").splitlines(keepends=True):
        if line.startswith("diff --git ") and cur:
            out.append("".join(cur)); cur = []
        cur.append(line)
    if cur:
        out.append("".join(cur))
    return out


def _path_of(chunk):
    m = re.match(r"diff --git a/(\S+)", chunk or "")
    return m.group(1) if m else ""


def _prose(r, client):
    """PR titles, bodies and comments."""
    empty = dict(pr1_title="", pr1_body="", pr2_title="", pr2_body="", discussion="")
    if client is None:
        return empty
    try:
        t = config.get_target(r["repo"])
        out, disc = dict(empty), []
        for tag, num in (("pr1", r.get("introducer_pr")), ("pr2", r.get("fixer_pr"))):
            if not num:
                continue
            page = client.get(f"/repos/{t.slug}/pulls/{num}", allow_404=True) or {}
            out[f"{tag}_title"] = page.get("title") or ""
            out[f"{tag}_body"] = page.get("body") or ""
            for c in client.issue_comments(t.slug, num):
                body = (c.get("body") or "").strip()
                if body:
                    disc.append(f"[{tag.upper()} comment] {body}")
        out["discussion"] = "\n".join(disc)
        return out
    except Exception:
        return empty


def redact(vars_, nums):
    """Hide the chain's own PR numbers."""
    pats = [re.compile(rf"#?\b{n}\b") for n in sorted(nums, reverse=True) if n]
    out = {}
    for k, v in vars_.items():
        s = str(v)
        for p in pats:
            s = p.sub("<PR-Number>", s)
        out[k] = s
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--repos", default="")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--no-schema", action="store_true")
    ap.add_argument("--no-write", action="store_true",
                    help="judge and record verdicts, but do not touch the master table")
    args = ap.parse_args()

    if not args.no_schema:
        rjp.EXTRA_BODY = dict(rjp.EXTRA_BODY, response_format={
            "type": "json_schema",
            "json_schema": {"name": "attribution", "schema": SCHEMA, "strict": True}})
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("set DEEPSEEK_API_KEY (and DEEPSEEK_BASE_URL / DEEPSEEK_MODEL)")
    on, off = rjp.probe_reasoning()
    print(f"reasoning probe: with-knob={on} chars, without={off}")
    if on <= 0:
        sys.exit("REASONING IS OFF. Set DEEPSEEK_EXTRA_BODY="
                 "'{\"chat_template_kwargs\": {\"thinking\": true}}' and retry.")

    rows = [json.loads(l) for l in open(MASTER)]
    rows = [r for r in rows if r.get("introducer_pr") and r.get("fixer_pr")]
    if args.repos:
        want = {x.strip() for x in args.repos.split(",") if x.strip()}
        rows = [r for r in rows if r["repo"] in want]
    done = {}
    if OUT.exists():
        for line in open(OUT):
            d = json.loads(line)
            if not (args.retry_errors and d.get("error")):
                done[d["instance_id"]] = d
    todo = [r for r in rows if r["instance_id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(rows)} chains, {len(done)} done, {len(todo)} to judge "
          f"({args.workers} workers, model={rjp.MODEL})", flush=True)

    token = secrets.github_token()
    clients, lock = {}, threading.Lock()

    def client_for(repo):
        with lock:
            if repo not in clients:
                clients[repo] = github_api.GitHubClient(token, repo) if token else None
            return clients[repo]

    fh = open(OUT, "a")
    wlock = threading.Lock()

    def judge(r):
        out = dict(instance_id=r["instance_id"], repo=r["repo"],
                   introducer_pr=r["introducer_pr"], fixer_pr=r["fixer_pr"])
        try:
            vars_, why = evidence(r, client_for(r["repo"]))
            if vars_ is None:
                out.update(code=None, skipped=why)
                return out
            vars_ = redact(vars_, {r["introducer_pr"], r["fixer_pr"]})
            out["capped_fields"] = [k for k, n in CAPS.items()
                                    if k in vars_ and len(vars_[k]) > n]
            for k, n in CAPS.items():
                if k in vars_:
                    vars_[k] = rjp.cap(vars_[k], n)
            reply = rjp.deepseek(rjp.render(PROMPT, **vars_))
            try:
                v = json.loads(reply.strip().removeprefix("```json")
                               .removeprefix("```").removesuffix("```").strip())
            except Exception:
                v = rjp.extract_json(reply) or {}
            code = (v.get("code") or "").strip().upper()
            if code not in CODES:
                out["error"] = f"unparseable code: {code!r}"
                return out
            out["code"] = code
            for k in ("defect", "pr1_role", "reasoning", "later_change", "evidence"):
                out[k] = v.get(k)
        except Exception as e:
            out["error"] = f"{e.__class__.__name__}: {e}"
        return out

    n = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(judge, todo):
            with wlock:
                fh.write(json.dumps(res) + "\n"); fh.flush()
            n += 1
            if n % 25 == 0:
                print(f"  ... {n}/{len(todo)}", flush=True)
    fh.close()
    print(f"\nwrote {OUT.relative_to(ROOT)}")

    # write verdicts to the szz block
    if args.no_write:
        print("--no-write: master table untouched")
        return
    verdicts = {}
    for line in open(OUT):
        d = json.loads(line)
        verdicts[d["instance_id"]] = d
    rows = store.load()
    updates = {}
    for row in rows:
        d = verdicts.get(row["instance_id"])
        if d is None:
            continue                      # not judged
        # create the block if missing
        blk = row.get("szz") if isinstance(row.get("szz"), dict) else {}
        att = {
            "code": d.get("code"),
            "holds": d.get("code") == "A" if d.get("code") else None,
            "reasoning": d.get("reasoning"),
            "skipped": d.get("skipped"),
            "error": d.get("error"),
        }
        if blk.get("attribution") != att:
            updates[row["instance_id"]] = dict(blk, attribution=att)
    if not updates:
        print("master table already up to date")
        return
    rep = store.update("szz", updates, rows=rows)
    print(f"master table: {len(rep.changed)} rows updated in the szz block")


if __name__ == "__main__":
    main()
