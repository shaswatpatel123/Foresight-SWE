#!/usr/bin/env python3
"""LLM agent that writes an install recipe per commit."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foresight import config
from foresight.langspec import changed_test_files

ROOT = config.REPO_ROOT
MANIFEST = ROOT / "docker" / "validate_instances.json"
OUT_DIR = ROOT / "docker" / "install"
RUNS = config.DATA_DIR / "installer_runs"
REGISTRY = "docker.io/shaswatpatel123/foresight"

DEFAULT_API = "http://localhost:8021/v1"
DEFAULT_MODEL = "deepseek-v4-flash"

# revisions allowed after a failed acceptance
ACCEPT_RETRIES = 2

# per-language preamble
PREAMBLE = {
    "pytest": """\
You are configuring a Python project's test environment inside a Linux container.

You are root. The repository is already cloned at {repo_dir} and checked out at commit
{commit} ({repo}). `uv` and `git` are installed. You have network access.""",
    "go": """\
You are configuring a Go project's test environment inside a Linux container.

You are root. The repository is already cloned at {repo_dir} and checked out at commit
{commit} ({repo}). Go {pyver} and `git` are installed and you have network access.
GOTOOLCHAIN=local is set deliberately: this image serves ONE planned Go version, so a
go.mod asking for a newer toolchain must fail loudly rather than silently downloading
one the plan never chose.""",
    "node": """\
You are configuring a JavaScript/TypeScript project's test environment inside a Linux
container.

You are root. The repository is already cloned at {repo_dir} and checked out at commit
{commit} ({repo}). Node {pyver}, `corepack`, `npm` and `git` are installed, and you
have network access.

USE THE PACKAGE MANAGER THIS COMMIT DECLARES, not the one you would pick. Read
package.json's `packageManager` field first; if it is absent, infer from the committed
lockfile -- pnpm-lock.yaml means pnpm, yarn.lock means yarn, package-lock.json means
npm, none means npm. Activate it through corepack, e.g.
`corepack prepare pnpm@8.6.3 --activate`. The manager and the lockfile format migrate
across this repo's history, so the commit decides, not the repo.

PREFER A FROZEN INSTALL -- `pnpm install --frozen-lockfile`, `npm ci`,
`yarn install --immutable`. The committed lockfile pins the whole transitive tree,
which is the strongest reproducibility guarantee available here. Fall back to a
non-frozen install ONLY if the frozen one fails (an old lockfile can reference a
since-yanked package, which no amount of cleverness fixes).

DO NOT add, remove or upgrade a dependency to make the tests run, and do not edit
package.json or the lockfile. The JUnit reporters (`jest-junit`, `mocha-junit-reporter`)
are already installed GLOBALLY and must stay that way -- installing one into the repo
would mutate the lockfile and destroy the frozen install.""",
}

# per-language rules
TOOLCHAIN_RULES = {
    "pytest": """\
- Inspect the repo's own declarations first (pyproject.toml [project.optional-dependencies]
  and [dependency-groups], setup.cfg [options.extras_require], requirements*.txt,
  tox.ini). Different commits of the same repo declare these DIFFERENTLY.
- PREFER commands that read the repo at the CURRENT commit -- `uv pip install -e '.[test]'`,
  `uv pip install -r requirements/test.txt` -- over listing package names by hand. A recipe
  that reads the repo stays correct at other commits; a hardcoded list does not.
- Create the venv at {venv} with: uv venv --python {pyver} {venv}
- Install with `uv pip install --python {venv}/bin/python ...`.""",
    "go": """\
- There is almost never anything to install. `go test` compiles from source, so the
  environment is just two warm caches: `go mod download all` fills $GOMODCACHE, and a
  `go build ./...` fills $GOCACHE so the first test run does not pay for it.
- If the build fails, the cause is usually a MISSING SYSTEM LIBRARY for a cgo package
  (sqlite, libgit2, libmagic), not a Go dependency. Install it with apt.
- Do NOT edit go.mod or go.sum, and do NOT run `go mod tidy`. That would change the
  commit's own dependency graph, which is the thing we are trying to reproduce.
- If a package needs a build tag to compile, note it -- but prefer leaving the default
  build configuration alone.
- There is no venv and nothing to create.""",
    "node": """\
- Inspect package.json FIRST: `packageManager`, `engines`, `scripts.test`, and which of
  vitest / jest / mocha is in devDependencies. Then check which lockfile is committed.
  Different commits of the same repo answer these DIFFERENTLY -- the framework and the
  package manager both migrate across the window.
- PREFER a lockfile-driven install (`pnpm install --frozen-lockfile`, `npm ci`,
  `yarn install --immutable`) over naming packages by hand. It stays correct at other
  commits; a hardcoded list does not.
- If the repo is a workspace/monorepo, install at the ROOT. Tests are run per package
  afterwards, but the dependency tree is resolved once at the top.
- There is NO venv here and nothing to create: `node_modules` lands in the repo, which
  is correct and expected. Do not try to install outside the worktree.""",
}

SYSTEM = """\
{preamble}

GOAL: find the commands that install this project and its TEST dependencies, so that
this exact command produces a JUnit XML file containing at least one <testcase>:

    {accept_cmd}

*** FAILING TESTS ARE EXPECTED AND ARE NOT YOUR PROBLEM. ***

You are configuring an ENVIRONMENT, not fixing code. Some of the test files in this
working tree were deliberately copied in from a LATER commit, so they test behaviour
this commit does not have yet. They are SUPPOSED to fail. A run that reports
"4 failed, 128 passed" is a COMPLETE SUCCESS -- the tests executed, which is all we
need. Report your config immediately when you see output like that.

You are done as soon as the command above RUNS the tests and writes the XML. You are
NOT done only when tests pass, and you must never try to make them pass. Do not read
or edit source files under src/, do not investigate an AssertionError, do not
downgrade or pin a package to make a test green.

The ONLY failures that mean your job is unfinished are ones that stop tests from
RUNNING at all:
  - ModuleNotFoundError / ImportError for a missing dependency
  - "no tests ran", an empty XML, or a usage error before collection
  - a collection error caused by an uninstalled pytest plugin

## Response format -- read this twice

Your response must contain a THOUGHT section and then EXACTLY ONE bash code block
holding ONE command (or commands joined with && or ||). Format it exactly as shown:

<format_example>
THOUGHT: Your reasoning and analysis here

```bash
your_command_here
```
</format_example>

The block MUST be a markdown fence opened with three backticks followed by `bash`
and closed with three backticks. Do NOT use XML-style tags such as <bash>...</bash>.
Do NOT emit more than one block. Anything else is unparseable, your command will not
run, and the turn is wasted.

Failure to follow these rules will cause your response to be rejected.

I will run the command and reply with its output. Rules:
{toolchain_rules}
- SYSTEM PACKAGES ARE YOURS TO INSTALL. You are root with network access, so if a
  build needs a compiler, a header, a library, or a toolchain the image does not
  have -- `apt-get update && apt-get install -y <pkg>` and carry on. A vendored C
  or JS dependency may need building before the package will compile (for example
  a submodule under vendor/ with its own Makefile or package.json); `git submodule
  update --init --recursive` and that build are legitimate steps too.
- EVERY command needed to reproduce the environment must appear in your final
  `install` list, including apt-get and any vendored build. The list is REPLAYED in
  a fresh container that has none of your changes -- a step you leave out is a step
  that will be missing when it matters.
- Prefer making the project's REAL build work over disabling it. An env var that
  skips the C extension (e.g. *_NO_EXTENSIONS=1) is a last resort, not a shortcut:
  it runs a different code path from the one the project ships and tests.
- Do not modify tracked files. Do not run `git checkout`, `git clean`, or `git commit`.
- Beware: a dependency that is itself a pytest PLUGIN may be incompatible with the
  version of the project under test at this commit. If the test command starts failing
  after you install something, remove it.

When the acceptance command works, reply with EXACTLY ONE fenced json block and nothing
else -- same fence rules, three backticks and `json`, no XML tags:

{output_spec}
"""

# final JSON format; node's test_cmd is run
OUTPUT_SPEC = {
    "pytest": """\
```json
{{"install": ["<cmd 1>", "<cmd 2>"], "test_cmd": ["<full-suite test command>"]}}
```

`install` must list ONLY the commands that were necessary and succeeded, in order,
starting with the `uv venv` command. `test_cmd` is the general (no file selectors)
command that runs the whole suite with per-test verbose output and no colour.

NOTE: `test_cmd` is PROVENANCE ONLY -- nothing downstream executes it. The grader
uses one canonical invocation per test framework, chosen by detecting the framework
at this commit, so that a single JUnit parser serves every repo and every language.
Your job is the ENVIRONMENT: make the acceptance command above work.""",
    "node": """\
```json
{{"install": ["<cmd 1>", "<cmd 2>"], "test_cmd": "<shell template, or omit>"}}
```

`install` must list ONLY the commands that were necessary and succeeded, in order.

`test_cmd` IS EXECUTED -- it is not documentation. Include it ONLY if running the
framework directly on a changed test file does not work. It is a SHELL SNIPPET run once
per test file with two variables set:

    $FILE   the test file path, relative to the repo root
    $JUNIT  the JUnit XML path it must write

OMIT `test_cmd` entirely when the changed file is itself a runnable test -- the default
invocation already handles that, and an unnecessary override is a liability.

Include it when the changed file is a FIXTURE: data that some other file turns into a
test. Symptom: the run reports 0 tests even though the install is fine. Then find the
file that actually contains the test code and write a snippet that derives it from
$FILE, so ONE template works for every chain in this repo. Worked example -- a repo
whose tests live in `<suite>/test.<ext>` and whose fixtures sit in
`<suite>/samples/<name>/`:

    SUITE="$(dirname "$(dirname "$(dirname "$FILE")")")/test.ts"; \\
    npx --no-install vitest run "$SUITE" \\
        --reporter=junit --outputFile="$JUNIT"

Derive from $FILE with dirname/basename -- do NOT hardcode one sample or one suite, or
the recipe only works for the chain you happened to test.

DO NOT NARROW THE RUN TO THE CHANGED TEST (no `-t`, no `--testNamePattern`, no single
sample). Run the whole file or suite that contains it and let us select afterwards.
Narrowing looks harmless and quietly destroys the PASS_TO_PASS set: the only tests
left running are the ones the PR changed, and those FAIL at the pre-fix commit by
definition, so nothing is ever observed to pass and the chain ships with no
no-regression signal. Measured on svelte -- filtered gave 3 usable outcomes and
p2p=0; unfiltered gave 2,995 and cost 19 extra seconds.""",

    "go": """\
```json
{{"install": ["<cmd 1>", "<cmd 2>"], "test_cmd": "<shell template, or omit>"}}
```

`install` must list ONLY the commands that were necessary and succeeded, in order.
For most Go commits that is one or two lines (`go mod download all`), and an empty
list is a legitimate answer when the toolchain alone suffices.

`install` must list ONLY the commands that were necessary and succeeded, in order.

`test_cmd` IS EXECUTED -- it is not documentation. Include it ONLY if running the
framework directly on a changed test file does not work. It is a SHELL SNIPPET run once
per test file with two variables set:

    $FILE   the test file path, relative to the repo root
    $JUNIT  the JUnit XML path it must write

OMIT `test_cmd` entirely when the changed file is itself a runnable test -- the default
invocation already handles that, and an unnecessary override is a liability.

Include it when the changed file is a FIXTURE: data that some other file turns into a
test. Symptom: the run reports 0 tests even though the install is fine. Then find the
file that actually contains the test code and write a snippet that derives it from
$FILE, so ONE template works for every chain in this repo. Worked example -- a repo
whose tests live in `<suite>/test.<ext>` and whose fixtures sit in
`<suite>/samples/<name>/`:

    SUITE="$(dirname "$(dirname "$(dirname "$FILE")")")/test.ts"; \\
    npx --no-install vitest run "$SUITE" \\
        --reporter=junit --outputFile="$JUNIT"

Derive from $FILE with dirname/basename -- do NOT hardcode one sample or one suite, or
the recipe only works for the chain you happened to test.

DO NOT NARROW THE RUN TO THE CHANGED TEST (no `-t`, no `--testNamePattern`, no single
sample). Run the whole file or suite that contains it and let us select afterwards.
Narrowing looks harmless and quietly destroys the PASS_TO_PASS set: the only tests
left running are the ones the PR changed, and those FAIL at the pre-fix commit by
definition, so nothing is ever observed to pass and the chain ships with no
no-regression signal. Measured on svelte -- filtered gave 3 usable outcomes and
p2p=0; unfiltered gave 2,995 and cost 19 extra seconds.""",
}


TIMED_OUT = "__FORESIGHT_TIMEOUT__"


def sh(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    """(rc, output); a timeout is a result."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        def dec(b) -> str:
            if b is None:
                return ""
            return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else b
        return 124, dec(e.stdout) + dec(e.stderr) + f"\n{TIMED_OUT}"


def chat(api: str, model: str, messages: list[dict], max_tokens: int = 8000,
         temperature: float = 0.0) -> str:
    """One chat completion."""
    # turn thinking on explicitly
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature,
                       "chat_template_kwargs": {"thinking": True}}).encode()
    req = urllib.request.Request(f"{api}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer EMPTY"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"].get("content") or ""


def extract(text: str, lang: str) -> str | None:
    m = re.findall(rf"```{lang}\s*\n(.*?)```", text, re.S)
    return m[-1].strip() if m else None


def one_action(text: str) -> tuple[str | None, str]:
    """(command, error): exactly one bash block."""
    blocks = [b.strip() for b in re.findall(r"```bash\s*\n(.*?)```", text, re.S)]
    blocks = [b for b in blocks if b]
    if len(blocks) == 1:
        return blocks[0], ""
    if not blocks:
        return None, ("Your response was REJECTED: no parseable command. It must "
                      "contain exactly ONE markdown code block opened with three "
                      "backticks followed by `bash` and closed with three backticks. "
                      "XML tags like <bash> are not accepted.")
    return None, (f"Your response was REJECTED: it contained {len(blocks)} bash "
                  f"blocks. Send EXACTLY ONE command (join steps with && if they "
                  f"belong together).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True)
    ap.add_argument("--manifest", default=str(MANIFEST),
                    help="which validate_instances manifest to read. Manifests are per LANGUAGE (validate_instances.json / validate_instances_javascript.json) because each describes the images of one plan.")
    ap.add_argument("--era", choices=["pr1", "pr2"], default="pr1")
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--api-base", default=DEFAULT_API)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-steps", type=int, default=100,
                    help="ceiling, not a target: a run stops two observations after the tests execute, so only genuinely hard builds get near this. The model is a local vLLM, so steps are cheap")
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()

    man = {m["instance_id"]: m for m in json.loads(Path(args.manifest).read_text())}
    m = man[args.instance]
    repo = m["repo"]
    t = config.get_target(repo)
    tf1 = changed_test_files(t.path, m["pr1_base_sha"], m["pr1_head_sha"], t.language)
    tf2 = changed_test_files(t.path, m["pr2_base_sha"], m["pr2_head_sha"], t.language)
    # pr1 must run both PR1 and PR2 tests
    if args.era == "pr1":
        commit = m["pr1_base_sha"]
        overlays = [(m["pr1_head_sha"], tf1), (m["pr2_head_sha"], tf2)]
        # dedupe, keep order
        tf = list(dict.fromkeys(tf1 + tf2))
    else:
        commit = m["pr2_head_sha"]
        overlays = []
        tf = list(dict.fromkeys(tf2))
    if not tf:
        print(f"{args.instance}/{args.era}: no test files for this era; nothing to accept against")
        sys.exit(2)

    img = m["images"]["pr1_base" if args.era == "pr1" else "pr2_head"]
    # version from the env tag
    if "-node" in (img or ""):
        pyver = (img or "").rsplit("-node", 1)[-1]
    elif "-go" in (img or ""):
        pyver = (img or "").rsplit("-go", 1)[-1]
    else:
        pyver = (img or "").rsplit("py", 1)[-1]
    pyver = pyver or "3.10"
    tag = f"{REGISTRY}:env-{img}"
    venv = "/opt/venv"
    # acceptance = the grader's command
    runner = m.get("runner", "pytest")
    if runner == "go":
        accept = f"/usr/local/bin/go_run_tests.sh /tmp/accept.xml {' '.join(tf)}"
    elif runner == "node":
        accept = f"/usr/local/bin/node_run_tests.sh /tmp/accept.xml {' '.join(tf)}"
    elif runner == "django":
        labels = sorted({t[len("tests/"):].removesuffix("/__init__.py")
                         .removesuffix(".py").replace("/", ".")
                         for t in tf if t.startswith("tests/") and t.endswith(".py")})
        accept = (f"{venv}/bin/python tests/runtests.py "
                  f"--settings={getattr(t, 'django_settings', 'test_sqlite')} "
                  f"--parallel 1 --verbosity 2 {' '.join(labels)} "
                  f"> /tmp/dj.log 2>&1; "
                  f"python3 /usr/local/bin/django_to_junit.py /tmp/dj.log /tmp/accept.xml")
    else:
        accept = (f"{venv}/bin/python -m pytest {' '.join(tf)} "
                  f"--junitxml=/tmp/accept.xml -p no:cacheprovider -q "
                  f"--continue-on-collection-errors")

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    logdir = RUNS / run_id / f"{args.instance}.{args.era}"
    logdir.mkdir(parents=True, exist_ok=True)

    cname = f"installer_{args.instance.replace('__','_')}_{args.era}_{int(time.time())}"
    rc, out = sh(["docker", "run", "-d", "--platform", args.platform, "--name", cname,
                  "--entrypoint", "/bin/sh", tag, "-c", "sleep infinity"])
    if rc != 0:
        print(f"could not start container: {out[:400]}", file=sys.stderr)
        sys.exit(1)

    # per-command timeout; longer for compiled
    COMPILED = {"numpy", "scipy", "pandas", "matplotlib", "sklearn", "astropy", "pillow"}
    cmd_timeout = 2700 if repo in COMPILED else 900

    def dexec(cmd: str, timeout: int | None = None) -> tuple[int, str]:
        return sh(["docker", "exec", "-w", "/testbed", cname, "bash", "-lc", cmd],
                  cmd_timeout if timeout is None else timeout)

    transcript: list[dict] = []
    try:
        rc, out = dexec(f"git checkout -f {commit} && git clean -fdxq && echo READY")
        if rc != 0:
            print(f"checkout failed: {out[-400:]}", file=sys.stderr)
            sys.exit(1)
        # overlay test files; quote every path
        for sha, files in overlays:
            if not files:
                continue
            rc, out = dexec(f"git checkout {sha} -- "
                            f"{' '.join(shlex.quote(f) for f in files)}")
            if rc != 0:
                print(f"overlay from {sha[:12]} failed: {out[-400:]}", file=sys.stderr)
                sys.exit(1)

        # django uses the pytest text
        key = runner if runner in PREAMBLE else "pytest"
        sysmsg = SYSTEM.format(
            preamble=PREAMBLE[key].format(repo_dir="/testbed", commit=commit,
                                          repo=t.slug, pyver=pyver),
            toolchain_rules=TOOLCHAIN_RULES[key].format(venv=venv, pyver=pyver),
            output_spec=OUTPUT_SPEC[key],
            repo_dir="/testbed", commit=commit, repo=t.slug,
            accept_cmd=accept, venv=venv, pyver=pyver)
        messages = [{"role": "system", "content": sysmsg},
                    {"role": "user", "content":
                     f"Begin. Test files that must run: {' '.join(tf)}"}]

        cfg = None
        ran_ok = 0            # times tests ran
        bad_fmt = 0           # unparseable replies in a row
        for step in range(1, args.max_steps + 1):
            # raise temperature when stuck on format
            reply = chat(args.api_base, args.model, messages,
                         temperature=min(0.2 * bad_fmt, 0.8))
            messages.append({"role": "assistant", "content": reply})
            transcript.append({"step": step, "assistant": reply})

            js = extract(reply, "json")
            if js:
                try:
                    cfg = json.loads(js)
                    print(f"[{step}] agent emitted install_config")
                    break
                except json.JSONDecodeError as e:
                    obs = f"That JSON did not parse ({e}). Re-emit one valid json block."
                    messages.append({"role": "user", "content": obs})
                    continue

            cmd, err = one_action(reply)
            if not cmd:
                bad_fmt += 1
                # give up after six in a row
                if bad_fmt >= 6:
                    print(f"[{step}] {bad_fmt} consecutive unparseable replies; aborting")
                    transcript.append({"step": step, "abort": "format_deadlock"})
                    break
                # reject and restate the format
                messages.append({"role": "user", "content":
                                 err + " Re-send that one command now, or the final "
                                 "```json block if you are done."})
                continue

            bad_fmt = 0        # parsed: reset
            rc, out = dexec(cmd)
            timed_out = TIMED_OUT in out
            out = out.replace(TIMED_OUT, "")[-4000:]
            print(f"[{step}] rc={rc}{' TIMEOUT' if timed_out else ''} "
                  f"$ {cmd.splitlines()[0][:90]}")
            transcript.append({"step": step, "cmd": cmd, "rc": rc,
                               "timed_out": timed_out, "out": out[-1500:]})
            if timed_out:
                # report the timeout
                messages.append({"role": "user", "content":
                                 f"The last command timed out and was killed:\n"
                                 f"<command>{cmd}</command>\n"
                                 f"Partial output:\n<output>\n{out}\n</output>\n"
                                 f"Try another command, and avoid anything that waits "
                                 f"for interactive input -- pass -y to apt-get, "
                                 f"--noconfirm/--yes to other installers, and never "
                                 f"start a REPL or a pager."})
                continue
            nudge = ""
            # tests ran: tell it to finish
            if re.search(r"\b\d+ (?:failed|passed)\b", out):
                ran_ok += 1
                nudge = ("\n\nNOTE: tests EXECUTED. That is the acceptance criterion -- "
                         "failures are expected here. Emit the final json block now "
                         "unless a dependency is still missing.")
            messages.append({"role": "user",
                             "content": f"exit code: {rc}\noutput:\n{out}{nudge}"})
            # ran twice: force finalize
            if ran_ok >= 2:
                print(f"[{step}] tests ran twice; forcing finalize")
                break

        # no config yet: ask up to 3 times
        for attempt in range(1, 4):
            if cfg:
                break
            insist = ("STOP. Do not run any more commands. The configuration phase is "
                      "over.\n\nReply with EXACTLY ONE ```json block and NOTHING else -- "
                      "no prose, no bash block, no verification step:\n\n"
                      '```json\n{"install": ["<cmd 1>", "<cmd 2>"], '
                      '"test_cmd": ["<full-suite command>"]}\n```\n\n'
                      "`install` lists only the commands that were necessary and "
                      "succeeded, in order, starting with `uv venv`.")
            messages.append({"role": "user", "content": insist})
            reply = chat(args.api_base, args.model, messages)
            messages.append({"role": "assistant", "content": reply})
            transcript.append({"step": f"finalize{attempt}", "assistant": reply})
            if (js := extract(reply, "json")):
                try:
                    cfg = json.loads(js)
                    print(f"[finalize{attempt}] agent emitted install_config")
                except json.JSONDecodeError:
                    pass

        # ---- acceptance: our command, same container
        result = {"instance_id": args.instance, "era": args.era, "repo": repo,
                  "commit": commit, "image": img, "test_files": tf,
                  "steps": step, "config": cfg}
        if cfg:
            # on failure, show output and let it revise
            for attempt in range(1, ACCEPT_RETRIES + 2):
                # pass node test_cmd via env
                pre = ""
                tc = cfg.get("test_cmd") if isinstance(cfg, dict) else None
                if runner == "node" and isinstance(tc, str) and tc.strip():
                    pre = f"export FORESIGHT_TEST_CMD={shlex.quote(tc)}; "
                rc, out = dexec(f"{pre}{accept}; echo '---XML---'; "
                                f"cat /tmp/accept.xml 2>/dev/null")
                head, _, xml = out.partition("---XML---")
                # don't count synthetic [bracketed] cases
                n = xml.count("<testcase") - len(re.findall(r'name="\[[^"]*\]"', xml))
                transcript.append({"step": f"accept{attempt}", "rc": rc,
                                   "testcases": n, "test_cmd": tc,
                                   "out": head[-2000:]})
                print(f"ACCEPTANCE[{attempt}]: {n} testcase(s) -> "
                      f"{'PASS' if n else 'FAIL'}")
                if n or attempt > ACCEPT_RETRIES:
                    break
                # show exactly what ran
                messages.append({"role": "user", "content":
                    f"Your config was applied and the acceptance command was run. It "
                    f"produced NO <testcase> elements, so it is not usable yet.\n\n"
                    f"Command we ran:\n<command>{accept}</command>\n"
                    + (f"\nYour test_cmd (run once per file with $FILE/$JUNIT set):\n"
                       f"<test_cmd>{tc}</test_cmd>\n" if tc else "")
                    + f"\nOutput:\n<output>\n{head[-3000:]}\n</output>\n\n"
                    f"Diagnose it. Common causes: unbalanced quotes or parens in a "
                    f"$(...) chain; a path that resolves to a DIRECTORY rather than a "
                    f"test file; a hardcoded path instead of one derived from $FILE; a "
                    f"reporter told to write somewhere other than $JUNIT. You may run "
                    f"bash commands to check (echo the derived path first), then reply "
                    f"with a corrected ```json block."})
                # tolerate format slips here too
                fixed, bad = None, 0
                for _ in range(6):
                    reply = chat(args.api_base, args.model, messages,
                                 temperature=min(0.2 * bad, 0.8))
                    messages.append({"role": "assistant", "content": reply})
                    transcript.append({"step": f"accept{attempt}.fix", "assistant": reply})
                    if (js := extract(reply, "json")):
                        try:
                            fixed = json.loads(js)
                            break
                        except json.JSONDecodeError as e:
                            messages.append({"role": "user", "content":
                                             f"That JSON did not parse ({e}). Re-emit "
                                             f"one valid json block."})
                            bad += 1
                            continue
                    cmd, err = one_action(reply)
                    if not cmd:
                        bad += 1
                        messages.append({"role": "user", "content":
                                         err + " You may run ONE bash command to "
                                         "investigate, or reply with the corrected "
                                         "```json block. Prose alone is not actionable."})
                        continue
                    bad = 0
                    rc2, out2 = dexec(cmd)
                    messages.append({"role": "user",
                                     "content": f"exit code: {rc2}\noutput:\n{out2[-3000:]}"})
                if not fixed:
                    break
                cfg = fixed
            result["config"] = cfg
            result["accept_rc"] = rc
            result["accept_testcases"] = n
            result["accepted"] = n > 0
            # keep output on failure
            if not n:
                result["accept_output"] = head[-4000:]
        else:
            result["accepted"] = False
            print("agent did not emit a config within the step budget")

        (logdir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        (logdir / "transcript.json").write_text(json.dumps(transcript, indent=2) + "\n")
        if result.get("accepted"):
            d = OUT_DIR / repo
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{commit[:12]}.json").write_text(json.dumps(cfg, indent=2) + "\n")
            print(f"-> docker/install/{repo}/{commit[:12]}.json")
        print(f"-> {logdir}")
        sys.exit(0 if result.get("accepted") else 1)
    finally:
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)


if __name__ == "__main__":
    main()
