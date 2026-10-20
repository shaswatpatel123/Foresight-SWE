#!/usr/bin/env python3
"""Singularity environment for Foresight (no conda)."""

import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from minisweagent.environments.singularity import (
    SingularityEnvironment,
    SingularityEnvironmentConfig,
)

# agent venv, outside /testbed
AGENT_VENV = "/opt/venv"
# default PATH with agent venv first
AGENT_PATH = f"{AGENT_VENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class ForesightSingularityEnvironmentConfig(SingularityEnvironmentConfig):
    base_commit: str | None = None
    """Commit the agent starts from."""
    no_localtime: bool = False
    """Apptainer's /etc/localtime bind corrupts zoneinfo."""
    net_isolate: bool = False
    """Private loopback network for this container."""
    codereview: bool = False
    """Apply round-1 patch after the git strip."""
    codereview_commit_msg: str = ""
    """Commit round-1 with this message."""


class ForesightSingularityEnvironment(SingularityEnvironment):
    """Conda-less Singularity env that starts the repo at base_commit."""

    def __init__(self, *, config_class: type = ForesightSingularityEnvironmentConfig, logger=None, **kwargs):
        # per-instance workdir: private /tmp
        self._workdir = Path(tempfile.gettempdir()) / f"apptainer-wd-{uuid.uuid4().hex[:8]}"
        self._workdir.mkdir(parents=True, exist_ok=True)
        # drop host binds that expose held-out tests
        self._env = {k: v for k, v in os.environ.items()
                     if k not in ("APPTAINER_BINDPATH", "BASH_ENV")}
        self._env["APPTAINER_WORKDIR"] = str(self._workdir)

        orig_skip_git_reset = kwargs.get("skip_git_reset", False)
        orig_init_patch = kwargs.get("init_patch", None)
        # parent must not strip .git or apply patch
        super().__init__(
            config_class=config_class, logger=logger,
            **{**kwargs, "skip_git_reset": True, "init_patch": None},
        )

        # move to the start commit
        if self.config.base_commit:
            # keep prebuilt env when reusing it
            clean = ('if [ "${FORESIGHT_REUSE_ENV:-0}" = "1" ] && '
                     '{ [ -x /opt/venv/bin/python ] || [ -d node_modules ]; }; '
                     'then git clean -fdq; else git clean -fdxq; fi')
            # skip checkout if at base; always clean
            head, _, _, hrc = self.exec_run_with_timeout(
                f"cd {self.config.cwd} && git rev-parse HEAD", timeout=120)
            at_base = hrc == 0 and head.strip().startswith(self.config.base_commit[:40])
            cmd = clean if at_base else f"git checkout -f {self.config.base_commit} && {clean}"
            out, _, _, rc = self.exec_run_with_timeout(
                f"cd {self.config.cwd} && {cmd}", timeout=1800,
            )
            if rc != 0:
                raise Exception(f"checkout base_commit {self.config.base_commit} failed: {out}")

        # apply round-1 patch before the strip
        if orig_init_patch and not self.config.codereview:
            for patch in orig_init_patch:
                self.apply_patch(patch)

        # agent phase: use or build a venv
        if self.config.base_commit and not orig_skip_git_reset:
            self._build_agent_venv()

        # agent phase: strip git history
        if not orig_skip_git_reset:
            self.reset_repo()

        # codereview: apply round-1 after the strip
        if orig_init_patch and self.config.codereview:
            for patch in orig_init_patch:
                self.apply_patch(patch)
            # agent phase only
            if self.config.codereview_commit_msg and not orig_skip_git_reset:
                # commit round-1 so HEAD~1 works
                msg = shlex.quote(self.config.codereview_commit_msg)
                out = self.execute(
                    f"cd {self.config.cwd} && git add -A && "
                    f"git -c user.email=foresight@local -c user.name=foresight "
                    f"commit -m {msg} --no-verify && git log --oneline -2"
                )
                # fail if the commit fails
                if out.get("returncode", 1) != 0:
                    raise RuntimeError(
                        f"codereview: committing the seeded round-1 patch failed: {out.get('output', '')}"
                    )
                self.logger.info(f"codereview: round-1 committed: {out.get('output', '')[-200:]}")

    def _build_agent_venv(self) -> None:
        """Use the image's venv, else build one."""
        _, _, _, rc = self.exec_run_with_timeout(
            f"test -x {AGENT_VENV}/bin/python", timeout=120)
        if rc == 0:
            self.config.env["VIRTUAL_ENV"] = AGENT_VENV
            self.config.env["PATH"] = AGENT_PATH
            # codex login shell resets PATH; use profile.d
            self.exec_run_with_timeout(
                f"printf '%s\\n' 'export VIRTUAL_ENV={AGENT_VENV}' "
                f"'export PATH={AGENT_VENV}/bin:$PATH' "
                f"> /etc/profile.d/zz-foresight-venv.sh", timeout=120)
            return

        out, _, _, rc = self.exec_run_with_timeout(
            f"bash /usr/local/bin/build_env.sh {AGENT_VENV}", timeout=1800)
        if rc != 0:
            self._logger_warn(f"agent venv build failed (agent will have no prebuilt env): {out[-2000:]}")
            return
        self.config.env["VIRTUAL_ENV"] = AGENT_VENV
        self.config.env["PATH"] = AGENT_PATH

    def _logger_warn(self, msg: str) -> None:
        lg = getattr(self, "logger", None)
        if lg is not None:
            lg.warning(msg)
        else:
            print(msg)

    def execute(self, command: str, cwd: str = "", timeout: int | None = None) -> dict:
        """Run a command in the container (no conda)."""
        cmd = [self.config.executable, "-s", "exec", "--fakeroot"]
        cmd.extend(["--contain", "--cleanenv"])

        if getattr(self.config, "no_localtime", False):
            cmd.extend(["--no-mount", "/etc/localtime"])

        # private network; FORESIGHT_NET_ISOLATE=0/1 forces it
        _force = os.getenv("FORESIGHT_NET_ISOLATE")
        if _force != "0" and (getattr(self.config, "net_isolate", False) or _force == "1"):
            cmd.extend(["--net", "--network=none"])

        if self.runtime_config.get("need_gpu", False):
            cmd.append("--nv")

        work_dir = cwd or self.config.cwd
        if work_dir and work_dir != "/":
            cmd.extend(["--pwd", work_dir])

        for key in self.config.forward_env:
            if (value := os.getenv(key)) is not None:
                cmd.extend(["--env", f"{key}={value}"])

        for key, value in self.config.env.items():
            cmd.extend(["--env", f"{key}={value}"])

        for key, value in self.runtime_config.get("env_vars", {}).items():
            cmd.extend(["--env", f"{key}={value}"])

        for export_str in self.runtime_config.get("env_exports", []):
            env_part = export_str.removeprefix("export ")
            if "=" in env_part:
                cmd.extend(["--env", env_part])

        # No conda prolog: foresight images use a baked uv venv, not conda.
        cmd.extend(["--writable", str(self.sandbox_dir), "bash", "-c", command])

        if os.getenv("FORESIGHT_DEBUG_CMD"):
            print("FORESIGHT_CMD:", " ".join(repr(c) for c in cmd), flush=True)

        timeout = self.config.timeout if timeout is None else timeout
        result = subprocess.run(
            cmd,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # pass per-instance workdir to child
            env=self._env,
        )
        return {"output": result.stdout, "returncode": result.returncode}

    def cleanup(self) -> None:
        """Remove the workdir, then the sandbox."""
        if os.getenv("FORESIGHT_KEEP_SANDBOX"):
            print(f"FORESIGHT_SANDBOX_KEPT: {self.sandbox_dir}", flush=True)
            return
        try:
            shutil.rmtree(self._workdir, ignore_errors=True)
        finally:
            sup = getattr(super(), "cleanup", None)
            if callable(sup):
                sup()
