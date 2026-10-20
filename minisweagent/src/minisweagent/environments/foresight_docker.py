#!/usr/bin/env python3
"""Docker environment for Foresight (no apptainer)."""

import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from minisweagent.run.extra.utils.exceptions import PatchApplyError

# agent venv, outside /testbed
AGENT_VENV = "/opt/venv"
# default PATH with agent venv first
AGENT_PATH = f"{AGENT_VENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class ForesightDockerEnvironmentConfig:
    image: str
    cwd: str = "/testbed"
    env: dict[str, str] = field(default_factory=dict)
    forward_env: list[str] = field(default_factory=list)
    timeout: int = 300
    base_commit: str | None = None
    skip_git_reset: bool = False
    init_patch: list | None = None
    files_to_change: list | None = None
    platform: str = "linux/amd64"
    executable: str = os.getenv("MSWEA_DOCKER_EXECUTABLE", "docker")


class ForesightDockerEnvironment:
    """Conda-less Docker env that starts the repo at base_commit."""

    def __init__(self, *, config_class: type = ForesightDockerEnvironmentConfig,
                 logger=None, **kwargs):
        import logging
        self.logger = logger or logging.getLogger("minisweagent.environment")
        # keep only known config fields
        allowed = {f.name for f in __import__("dataclasses").fields(config_class)}
        self.config = config_class(**{k: v for k, v in kwargs.items() if k in allowed})

        # strip docker:// prefix
        image = self.config.image
        if isinstance(image, str) and image.startswith("docker://"):
            image = image[len("docker://"):]
        self.image = image

        self.container = f"foresight-{uuid.uuid4().hex[:10]}"
        # no host sandbox under docker
        self.sandbox_dir = None

        self._start_container()

        # check out the start commit
        if self.config.base_commit:
            out, _, _, rc = self.exec_run_with_timeout(
                f"cd {self.config.cwd} && git checkout -f {self.config.base_commit} && git clean -fdxq",
                timeout=600,
            )
            if rc != 0:
                raise Exception(f"checkout base_commit {self.config.base_commit} failed: {out}")

        # apply round-1 patch, if any
        if self.config.init_patch:
            for patch in self.config.init_patch:
                self.apply_patch(Path(patch))

        # agent phase: prebuild a venv
        if self.config.base_commit and not self.config.skip_git_reset:
            self._build_agent_venv()

        # agent phase: strip git history
        if not self.config.skip_git_reset:
            self.reset_repo()

    def _build_agent_venv(self) -> None:
        """Build /opt/venv at the current (base_commit) checkout and put it on PATH."""
        out, _, _, rc = self.exec_run_with_timeout(
            f"bash /usr/local/bin/build_env.sh {AGENT_VENV}", timeout=1800)
        if rc != 0:
            # non-fatal: agent builds its own env
            self.logger.warning(
                f"agent venv build failed (agent will have no prebuilt env): {out[-2000:]}")
            return
        self.config.env["VIRTUAL_ENV"] = AGENT_VENV
        self.config.env["PATH"] = AGENT_PATH

    # -- container lifecycle -----------------------------------------------------
    def _start_container(self) -> None:
        subprocess.run(
            [self.config.executable, "run", "-d", "--platform", self.config.platform,
             "--name", self.container, "--entrypoint", "/bin/sh", self.image,
             "-c", "sleep infinity"],
            text=True, check=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def reset_repo(self):
        _o, _t, _e, rc = self.exec_run_with_timeout(f"cd {self.config.cwd} && rm -rf .git", timeout=600)
        if rc != 0:
            raise Exception(f"Removing .git failed: {_o}")
        # don't re-encode fixtures on git add
        _o, _t, _e, rc = self.exec_run_with_timeout(
            f"cd {self.config.cwd} && git init "
            f"&& printf '* -working-tree-encoding\\n' > .git/info/attributes "
            f"&& git config user.email 'foresight@example.com' "
            f"&& git config user.name 'foresight' && git add . "
            f"&& git commit --allow-empty -m 'Clean benchmark snapshot' --no-verify",
            timeout=600,
        )
        if rc != 0:
            raise Exception(f"Reset repo failed: {_o}")

    # -- execution ---------------------------------------------------------------
    def execute(self, command: str, cwd: str = "", timeout: int | None = None) -> dict[str, Any]:
        cmd = [self.config.executable, "exec"]
        work_dir = cwd or self.config.cwd
        if work_dir:
            cmd.extend(["-w", work_dir])
        # forward host env vars, then fixed env
        for key in self.config.forward_env:
            if (value := os.getenv(key)) is not None:
                cmd.extend(["-e", f"{key}={value}"])
        for key, value in self.config.env.items():
            cmd.extend(["-e", f"{key}={value}"])
        cmd.extend([self.container, "bash", "-c", command])

        timeout = self.config.timeout if timeout is None else timeout
        result = subprocess.run(
            cmd, text=True, timeout=timeout, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return {"output": result.stdout, "returncode": result.returncode}

    def exec_run_with_timeout(self, cmd, timeout: int | None = 60):
        start = time.time()
        try:
            result = self.execute(cmd, timeout=timeout)
            return result["output"], False, time.time() - start, result["returncode"]
        except subprocess.TimeoutExpired as e:
            output = e.stdout if e.stdout else ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return output, True, time.time() - start, -1

    # -- patch handling (grade phase) -------------------------------------------
    def copy_to_container(self, src: Path, dst: PurePosixPath):
        subprocess.run(
            [self.config.executable, "cp", str(src), f"{self.container}:{dst}"],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    def apply_patch(self, patch_file: Path):
        patch_in = f"{self.config.cwd}/patch.diff"
        self.copy_to_container(Path(patch_file), PurePosixPath(patch_in))
        cmds = ["git apply --verbose", "git apply --verbose --reject", "patch --batch --fuzz=5 -p1 -i"]
        messages = {}
        for c in cmds:
            val = self.execute(f"cd {self.config.cwd} && {c} {patch_in}")
            if val["returncode"] == 0:
                self.execute(f"rm -rf {patch_in}")
                return
            messages[c] = val["output"]
        Path(patch_file).with_name(Path(patch_file).name + ".error2").write_text(str(messages))
        raise PatchApplyError()

    def get_template_vars(self) -> dict[str, Any]:
        return asdict(self.config)

    # -- cleanup -----------------------------------------------------------------
    def cleanup(self):
        if getattr(self, "container", None):
            subprocess.run([self.config.executable, "rm", "-f", self.container],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.container = None

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
