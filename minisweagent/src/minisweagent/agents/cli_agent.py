#!/usr/bin/env python3
"""Run Claude Code or Codex as the agent."""

import dataclasses
import json
import os
import shlex
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from minisweagent import Environment
from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.cli_trajectory import parse_cli_trajectory
from minisweagent.utils.log import logger

# CLI home inside the container
_CONTAINER_HOME = "/opt/agent_home"

# pull review findings from codex rollout
_CODEX_FINDINGS_JS = r"""
const fs = require("fs"), p = require("path");
const root = process.argv[1], since = Number(process.argv[2] || 0);
let files = [];
(function walk(dir) {
  let ents = [];
  try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
  for (const e of ents) {
    const f = p.join(dir, e.name);
    if (e.isDirectory()) walk(f);
    else if (e.name.endsWith(".jsonl") && fs.statSync(f).mtimeMs >= since) files.push(f);
  }
})(root);
files.sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
for (const f of files) {
  let hit = null;
  for (const line of fs.readFileSync(f, "utf8").split("\n")) {
    // the object lives inside a JSON string, so quotes are escaped on the raw line
    if (!line.includes("findings")) continue;
    let j; try { j = JSON.parse(line); } catch (e) { continue; }
    const pl = j.payload || {};
    if (pl.type !== "message" || pl.role !== "assistant") continue;
    const t = (pl.content || []).map(c => c.text || "").join("").trim();
    try { const o = JSON.parse(t); if (Array.isArray(o.findings)) hit = o.findings; } catch (e) {}
  }
  if (hit) { console.log(JSON.stringify(hit, null, 2)); process.exit(0); }
}
console.log("[]");
"""

# prompt file, git-excluded from the diff
_PROMPT_FILENAME = ".foresight_prompt.txt"

# set once the usage limit is hit
USAGE_LIMIT_HIT = threading.Event()

# usage-limit error strings
_USAGE_LIMIT_SIGNATURES = (
    "usage_limit_reached",
    "rate_limit_exceeded",
    "usage limit",
    '"code":429',
    '"status":429',
    "status_code=429",
    "too many requests",
)
# only scan error events
_ERROR_EVENT_MARKERS = ("turn.failed", "stream_error", "thread.error",
                        '"type":"error"', '"is_error":true', '"subtype":"error')


def _detect_usage_limit(raw: str) -> str | None:
    """Error line if usage limit hit, else None."""
    if not raw:
        return None
    for line in raw.splitlines():
        low = line.lower()
        if not any(m in low for m in _ERROR_EVENT_MARKERS):
            continue
        if any(sig in low for sig in _USAGE_LIMIT_SIGNATURES):
            return line.strip()[:500]
    return None


# Per-backend defaults: npm package, CLI binary, and the model slug passed to it.
_BACKENDS = {
    "claude_code": {
        "package": "@anthropic-ai/claude-code",
        "binary": "claude",
        "default_model": "claude-sonnet-4-5-20250929",
        # API key or OAuth token
        "forward_env": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"],
    },
    "codex": {
        "package": "@openai/codex",
        "binary": "codex",
        # must be a model the account supports
        "default_model": "gpt-5.5",
        "forward_env": ["OPENAI_API_KEY", "OPENAI_BASE_URL"],
    },
}

# install node: apt, else tarball
DEFAULT_INSTALL_SCRIPT = r"""
set -e
if ! command -v node >/dev/null 2>&1; then
  if ! {
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq curl ca-certificates xz-utils >/dev/null
      curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
      apt-get install -y -qq nodejs >/dev/null
    } >/dev/null 2>&1; then
    NODE_VER=v22.14.0
    NODE_DIR=/usr/local/lib/nodejs/node-${NODE_VER}-linux-x64
    mkdir -p /usr/local/lib/nodejs
    curl -fsSL --retry 3 -o /tmp/node.tar.gz \
      "https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-linux-x64.tar.gz"
    tar -xzf /tmp/node.tar.gz -C /usr/local/lib/nodejs
    rm -f /tmp/node.tar.gz
    for b in node npm npx; do ln -sf "$NODE_DIR/bin/$b" "/usr/local/bin/$b"; done
    # install CLI into /usr/local
    export NPM_CONFIG_PREFIX=/usr/local
    export PATH="$NODE_DIR/bin:/usr/local/bin:$PATH"
  fi
fi
node --version
"""


@dataclass
class CLIAgentConfig:
    backend: str = "claude_code"
    """Which native CLI to drive: 'claude_code' or 'codex'."""
    model_name: str = ""
    """Bookkeeping name written to preds.json / trajectory. Defaults to cli_model."""
    cli_model: str = ""
    """Model slug passed to the CLI (--model). Defaults to the backend default."""
    max_turns: int = 250
    """Passed to `claude --max-turns`; advisory only for codex."""
    cost_limit: float = 0.0
    """Recorded for parity with DefaultAgent; not enforced in-loop (the CLI owns its loop)."""
    timeout: int = 3600
    """Wall-clock budget (s) for the single CLI invocation."""
    install_script: str = DEFAULT_INSTALL_SCRIPT
    """Shell snippet run before the CLI to provision node (and anything else)."""
    extra_args: str = ""
    """Verbatim extra CLI flags appended to the invocation."""
    auth_file: str = ""
    """Host path to a file credential staged into the container (codex: ~/.codex/auth.json)."""
    cli_version: str = ""
    """Pinned npm version of the CLI."""
    resume_session_file: str = ""
    """Saved Claude session to resume (claude_code only)."""
    codex_review: bool = False
    """codex only: `codex review` then apply its findings via exec."""
    apply_review_template: str = ""
    """Apply prompt used with codex_review."""
    cli_instance_template: str = (
        "{{task}}\n\n"
        "You are working in the git repository at {{cwd}}. Implement the source changes "
        "needed to satisfy the request above.\n"
        "Guidelines:\n"
        "- MODIFY only regular (non-test) source files under {{cwd}}.\n"
        "- DO NOT modify tests or configuration files (pyproject.toml, setup.cfg, tox.ini, etc.).\n"
        "- Make the fix general and consistent with the surrounding codebase.\n"
        "- Do NOT create git commits or branches; leave your changes in the working tree.\n"
        "When you have finished and verified your change, stop."
    )


@dataclass
class _ShimModelConfig:
    # dataclass so save_traj's dataclasses.asdict(model.config) is serializable.
    model_name: str
    backend: str
    cli_model: str


class _ShimModel:
    """Stand-in model for bookkeeping."""

    def __init__(self, model_name: str, backend: str, cli_model: str):
        self.config = _ShimModelConfig(model_name, backend, cli_model)
        self.cost = 0.0
        self.n_calls = 0

    def get_template_vars(self) -> dict:
        return {}

    def query(self, *args, **kwargs):  # pragma: no cover - never used; CLI owns the loop
        raise NotImplementedError("CLIAgent drives a native CLI; it does not call model.query().")


class CLIAgent(DefaultAgent):
    """Drives a native coding CLI (Claude Code / Codex) to produce a patch."""

    def __init__(self, env: Environment, *, backend: str = "claude_code",
                 progress_manager=None, instance_id: str = "",
                 artifact_dir: Path | None = None, **kwargs):
        if backend not in _BACKENDS:
            raise ValueError(f"unknown CLI backend {backend!r}; expected one of {list(_BACKENDS)}")
        # keep only known config fields
        allowed = {f.name for f in dataclasses.fields(CLIAgentConfig)}
        cfg_kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        cfg_kwargs["backend"] = backend

        spec = _BACKENDS[backend]
        cli_model = cfg_kwargs.get("cli_model") or spec["default_model"]
        model_name = cfg_kwargs.get("model_name") or cli_model
        cfg_kwargs["cli_model"] = cli_model
        cfg_kwargs["model_name"] = model_name

        self.config = CLIAgentConfig(**cfg_kwargs)
        # Set by _stage_resume_session() during run(); "" means a cold start.
        self._resume_uuid: str = ""
        self.messages = []
        self.model = _ShimModel(model_name, backend, cli_model)
        self.env = env
        self.extra_template_vars = {}
        self.progress_manager = progress_manager
        self.instance_id = instance_id
        self.artifact_dir = Path(artifact_dir) if artifact_dir is not None else None

    # -- helpers -----------------------------------------------------------------
    def _spec(self) -> dict:
        return _BACKENDS[self.config.backend]

    def _install_cli_cmd(self) -> str:
        """npm-install the CLI package after node is provisioned (logs on failure)."""
        pkg = self._spec()["package"]
        binary = self._spec()["binary"]
        # pinned: always install
        if self.config.cli_version:
            pinned = shlex.quote(f"{pkg}@{self.config.cli_version}")
            return (
                f"npm install -g {pinned} >/tmp/npm_install.log 2>&1 || "
                f'{{ echo NPM_INSTALL_FAILED; tail -40 /tmp/npm_install.log; }}; '
                f"command -v {binary} >/dev/null 2>&1 || echo CLI_BINARY_MISSING:{binary}; "
                f"{binary} --version"
            )
        return (
            f"if ! command -v {binary} >/dev/null 2>&1; then "
            f"npm install -g {shlex.quote(pkg)} >/tmp/npm_install.log 2>&1 || "
            f'{{ echo NPM_INSTALL_FAILED; tail -40 /tmp/npm_install.log; }}; fi; '
            f"command -v {binary} >/dev/null 2>&1 || echo CLI_BINARY_MISSING:{binary}"
        )

    def _ensure_forward_env(self) -> None:
        """Make the CLI's API-key env vars cross the --contain/--cleanenv boundary."""
        fwd = self.env.config.forward_env
        # with OAuth token, don't forward API key
        skip = {"ANTHROPIC_API_KEY"} if os.getenv("CLAUDE_CODE_OAUTH_TOKEN") else set()
        for var in self._spec()["forward_env"]:
            if var not in fwd and var not in skip:
                fwd.append(var)
        # also drop it from the YAML list
        if skip:
            self.env.config.forward_env = [v for v in fwd if v not in skip]

    def _stage_auth(self) -> None:
        """Copy codex auth.json into the container."""
        if self.config.backend != "codex":
            return
        src = Path(self.config.auth_file or "~/.codex/auth.json").expanduser()
        if not src.is_file():
            self.add_message("user", f"[warn] codex auth file not found at {src}; "
                                     "codex will fall back to OPENAI_API_KEY if forwarded")
            return
        dest_dir = f"{_CONTAINER_HOME}/.codex"
        self.env.execute(f"mkdir -p {dest_dir}")
        self.env.copy_to_container(src, PurePosixPath(f"{dest_dir}/auth.json"))

    def _stage_resume_session(self) -> str:
        """Stage a saved session; return its uuid."""
        if self.config.backend != "claude_code" or not self.config.resume_session_file:
            return ""
        src = Path(self.config.resume_session_file)
        if not src.is_file():
            self.add_message("user", f"[warn] resume session not found at {src}; starting cold")
            return ""
        cwd = (self.env.config.cwd or "/").rstrip("/")
        encoded = cwd.replace("/", "-") or "-"
        dest_dir = f"{_CONTAINER_HOME}/.claude/projects/{encoded}"
        self.env.execute(f"mkdir -p {dest_dir}")
        self.env.copy_to_container(src, PurePosixPath(f"{dest_dir}/{src.name}"))
        return src.stem  # the CLI names the file <session-uuid>.jsonl

    def _build_invocation(self) -> str:
        """Build the headless CLI command."""
        binary = self._spec()["binary"]
        model = self.config.cli_model
        extra = self.config.extra_args
        prompt_path = _PROMPT_FILENAME
        if self.config.backend == "claude_code":
            # stream-json: full trajectory; last line has cost
            resume = f"--resume {shlex.quote(self._resume_uuid)} " if self._resume_uuid else ""
            return (
                f"cat {prompt_path} | {binary} {resume}-p --output-format stream-json --verbose "
                f"--dangerously-skip-permissions --model {shlex.quote(model)} "
                f"--max-turns {int(self.config.max_turns)} {extra}"
            )
        # codex: prompt via stdin, not an argument
        model_flag = f"--model {shlex.quote(model)} " if model else ""
        if self.config.codex_review:
            # review, then pipe findings into exec
            sessions = f"{_CONTAINER_HOME}/.codex/sessions"
            review_log = f"{_CONTAINER_HOME}/codex_review_stdout.txt"
            js = shlex.quote(_CODEX_FINDINGS_JS)
            review_model = f"-c model={shlex.quote(model)} " if model else ""
            return (
                f"{{ _t=$(date +%s)000; "
                f"{binary} review -c sandbox_mode=\"danger-full-access\" "
                f"-c approval_policy=\"never\" {review_model}"
                f"--commit HEAD --title \"$(git log -1 --format=%s)\" "
                f"> {shlex.quote(review_log)} 2>&1 || true; "
                f"cat {prompt_path}; echo; echo '--- review findings (JSON) ---'; echo; "
                f"node -e {js} {shlex.quote(sessions)} \"$_t\"; }}"
                f" | {binary} exec - --skip-git-repo-check --json "
                f"--dangerously-bypass-approvals-and-sandbox {model_flag}{extra}"
            )
        return (
            f"cat {prompt_path} | {binary} exec - --skip-git-repo-check --json "
            f"--dangerously-bypass-approvals-and-sandbox {model_flag}{extra}"
        )

    def _write_prompt_cmd(self, prompt: str) -> str:
        # exclude prompt from git, then write it
        return (
            "mkdir -p .git/info && "
            f"grep -qxF {shlex.quote(_PROMPT_FILENAME)} .git/info/exclude 2>/dev/null || "
            f"printf '%s\\n' {shlex.quote(_PROMPT_FILENAME)} >> .git/info/exclude\n"
            f"cat > {shlex.quote(_PROMPT_FILENAME)} <<'FORESIGHT_PROMPT_EOF'\n"
            f"{prompt}\n"
            "FORESIGHT_PROMPT_EOF"
        )

    def _last_json(self, stdout: str):
        text = stdout.strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        return None

    def _parse_usage(self, stdout: str) -> None:
        try:
            if self.config.backend == "claude_code":
                # stream-json: the final {"type":"result",...} line has cost + turns.
                obj = self._last_json(stdout)
                if obj:
                    self.model.cost = float(obj.get("total_cost_usd") or 0.0)
                    self.model.n_calls = int(obj.get("num_turns") or 0)
            else:  # codex JSONL: count event lines as a rough call proxy
                self.model.n_calls = sum(
                    1 for ln in stdout.splitlines() if ln.strip().startswith("{")
                )
        except Exception:
            pass

    def _collect_artifacts(self, raw_stdout: str, traj=None) -> None:
        """Save CLI output and native sessions."""
        if self.artifact_dir is None:
            return
        try:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            iid = self.instance_id or "instance"
            (self.artifact_dir / f"{iid}.cli_stdout.jsonl").write_text(raw_stdout)
            if traj is not None:
                (self.artifact_dir / f"{iid}.trajectory.json").write_text(
                    json.dumps(traj.as_dict(), indent=2, default=str))
            sandbox = getattr(self.env, "sandbox_dir", None)
            if sandbox is None:
                return
            home = Path(sandbox) / _CONTAINER_HOME.lstrip("/")
            native_globs = {
                "claude_code": "**/.claude/projects/**/*.jsonl",
                "codex": "**/.codex/sessions/**/*.jsonl",
            }[self.config.backend]
            dest = self.artifact_dir / f"{iid}_native_sessions"
            for src in home.glob(native_globs):
                if src.is_file():
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest / src.name)
            # keep the review transcript
            review_log = home / "codex_review_stdout.txt"
            if review_log.is_file():
                shutil.copy2(review_log, self.artifact_dir / f"{iid}.codex_review_stdout.txt")
        except Exception:
            pass  # artifacts are best-effort; never fail the run over them

    # -- main entrypoint ---------------------------------------------------------
    def run(self, task: str, **kwargs) -> tuple[str, str]:
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        # usage limit already hit: skip
        if USAGE_LIMIT_HIT.is_set():
            self.add_message("user", "[skipped] provider usage window exhausted earlier in this batch")
            return "usage_limit_reached", ""
        # codex_review: prompt is the apply step only
        template = self.config.apply_review_template if self.config.codex_review \
            else self.config.cli_instance_template
        prompt = self.render_template(template)
        self.add_message("user", prompt)
        self._ensure_forward_env()
        self._stage_auth()
        # Must precede _build_invocation(): it decides whether --resume is passed.
        self._resume_uuid = self._stage_resume_session()

        cwd = self.env.config.cwd
        if self.progress_manager is not None:
            self.progress_manager.update_instance_status(self.instance_id, f"running {self.config.backend}")

        # Snapshot SHA so the diff is robust to CLI-made intermediate commits.
        snap_out = self.env.execute(f"cd {cwd} && git rev-parse HEAD", timeout=120)
        lines = snap_out["output"].strip().splitlines()
        snapshot = lines[-1].strip() if lines else ""
        if snap_out["returncode"] != 0 or len(snapshot) < 7:
            return "cli_snapshot_failed", snap_out["output"]

        full = "\n".join([
            # lets claude skip permissions as root
            f"export HOME={_CONTAINER_HOME} && export IS_SANDBOX=1 && mkdir -p {_CONTAINER_HOME}",
            f"cd {cwd}",
            # after cd: writes the prompt file + .git/info/exclude in the repo cwd.
            self._write_prompt_cmd(prompt),
            self.config.install_script,
            self._install_cli_cmd(),
            self._build_invocation(),
        ])
        run_out = self.env.execute(full, timeout=self.config.timeout)
        raw = run_out["output"]
        self._parse_usage(raw)

        # usage limit: stop the rest of the batch
        limit_line = _detect_usage_limit(raw)
        if limit_line:
            USAGE_LIMIT_HIT.set()
            logger.error(
                f"{self.instance_id}: provider USAGE LIMIT hit -> {limit_line}\n"
                "  Latched batch: remaining instances short-circuit. Re-run the same "
                "command after the window resets — preds.json + grading are idempotent."
            )

        # parse CLI events into steps
        traj = None
        try:
            traj = parse_cli_trajectory(self.config.backend, text=raw)
        except Exception:
            traj = None
        if traj is not None and traj.steps:
            self.messages.extend(traj.to_messages())
        else:
            self.add_message("assistant", raw)
        self._collect_artifacts(raw, traj)

        # Capture the full delta vs the snapshot (staged + unstaged + untracked).
        diff_cmd = f"cd {cwd} && git add -A && git diff --cached {snapshot}"
        diff_out = self.env.execute(diff_cmd, timeout=300)
        diff = diff_out["output"]
        if diff_out["returncode"] != 0:
            self.add_message("user", f"diff capture failed (rc={diff_out['returncode']})")
            return "cli_diff_failed", diff
        if limit_line:
            return "usage_limit_reached", diff
        return "Submitted", diff
