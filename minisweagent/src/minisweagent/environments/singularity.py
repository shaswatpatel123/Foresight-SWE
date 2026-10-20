#!/usr/bin/env python3

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
import stat

from minisweagent.run.extra.utils.exceptions import PatchApplyError


@dataclass
class SingularityEnvironmentConfig:
    image: str
    cwd: str = "/"
    env: dict[str, str] = field(default_factory=dict)
    """Environment variables to set in the container."""
    forward_env: list[str] = field(default_factory=list)
    """Environment variables to forward to the container."""
    timeout: int = 30
    """Timeout for executing commands in the container."""
    executable: str = os.getenv("MSWEA_SINGULARITY_EXECUTABLE", "singularity")
    """Path to the singularity executable."""
    files_to_change: list[str] | None = None
    """Path to the patch to apply during init of sandbox"""
    init_patch: list[Path] | None = None
    """Singularity runtime config"""
    runtime_config: dict = field(default_factory=dict)
    multilingual: bool = False
    """Whether to run multilingual evaluation."""
    skip_git_reset: bool = False
    """Skip .git removal — needed for run-only-eval so entryscripts can git checkout test files."""

class SingularityEnvironment:
    def __init__(
        self, *, config_class: type = SingularityEnvironmentConfig, logger: logging.Logger | None = None, **kwargs
    ):

        """Singularity environment. See `SingularityEnvironmentConfig` for kwargs."""

        self.logger = logger or logging.getLogger("minisweagent.environment")
        self.config = config_class(**kwargs)
        self.sandbox_dir = Path(tempfile.gettempdir()) / f"minisweagent-{uuid.uuid4().hex[:8]}"

        files_to_change = kwargs.get( 'files_to_change', None )
        init_patch = kwargs.get( 'init_patch', None )
        self.multilingual = kwargs.get( 'multilingual', False )
        self.runtime_config = kwargs.get( 'runtime_config', {} )

        self._setup_sandbox(files_to_change, init_patch)
        
        # Create a branch and commit the changes
        # This will prevent any issue when the model does "git --checkout <file.py>"
        # We only do this for files we have either changed by copy-pasting(PR_0) or changed by patch(PR_1).
        if files_to_change is not None or init_patch is not None:
            _res = self.execute("git checkout -b foresight")
            if _res['returncode'] == 1:
                raise Exception("git checkout failed")
            self.execute("git config user.email 'foresight@example.com'")
            self.execute("git config user.name 'foresight'")
            _, _, _, _returncode = self.exec_run_with_timeout("git add .", timeout=180)
            if _returncode == 1:
                raise Exception("git add failed")
            _res = self.execute("git commit -m 'Initial setup' --no-verify")
            if _res['returncode'] != 0 and "nothing to commit" not in _res['output']:
                raise Exception("git commit failed")
        # NOTE: If you want to extend this to more than 2 PRs, you need to change the code
        # init_patch will become a list of paths and you need to apply the patches in the order of the list
        # At the end, the environment will be in the state of the last PR in the list and ready to be used!
        # The list will have [PR_0, PR_1, PR_2, ... PR_(n-1)] as PR_n will be applied in the swebench.py eval code!

        #NOTE: Remove `.git` files - agent sometime peeks future/past commits. This prevents it.
        if not self.config.skip_git_reset:
            self.reset_repo()

    def reset_repo(self):
        _stdout, _stderr, _, _returncode = self.exec_run_with_timeout(f"cd {self.config.cwd} && rm -rf .git", timeout=3600)
        if _returncode != 0:
            raise Exception(f"Removing .git file failed, {_stderr}")
        # $GIT_DIR/info/attributes has the HIGHEST attribute precedence (above the in-tree
        # .gitattributes); '* -working-tree-encoding' unsets working-tree-encoding so `git add`
        # won't try to transcode intentionally mis-encoded fixtures (e.g. iso-8859 or
        # windows-1251 test files) and abort the
        # snapshot with "fatal: failed to encode ... to UTF-8". Non-destructive: it doesn't
        # touch the tracked .gitattributes or the on-disk files (git add never rewrites the
        # working tree), so encoding-sensitive tests still pass. No-op for repos without the
        # attribute. Verified in a real sandbox (OLD: exit 128 encode-fail; NEW: exit 0).
        _stdout, _stderr, _, _returncode = self.exec_run_with_timeout(f"cd {self.config.cwd} && git init && printf '* -working-tree-encoding\\n' > .git/info/attributes && git config user.email 'foresight@example.com' && git config user.name 'foresight' && git add . && git commit --allow-empty -m 'Clean benchmark snapshot' --no-verify", timeout=3600)
        if _returncode != 0:
            raise Exception(f"Reset the repo failed: stdout={_stdout}, stderr={_stderr}")

    def _setup_sandbox(self, files_to_change: list[str] | None = None, init_patch: list[Path] | None = None):
        subprocess.run(
            [self.config.executable, "build", "--fakeroot", "--sandbox", self.sandbox_dir, self.config.image],
            text=True,
            check=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Sometimes apptainer will not be able to bind necessary folders from host to container                                                                                                                                                  
        # This will prevent --writeable and will cause in error!                                                                                                                                                                                 
        for p in ["scratch", "projects", "share/apps", "state/partition1"]:                                                                                                                                                                      
            os.makedirs(os.path.join(self.sandbox_dir, p), exist_ok=True)                                                                                                                                                                        
                                                                                                                                                                                                                                                 
        # optional: cache dirs inside container filesystem (only matters if you don't bind them)                                                                                                                                                 
        for p in ["tmp", "var/tmp", "root/.cache"]:                                                                                                                                                                                              
            os.makedirs(os.path.join(self.sandbox_dir, p), exist_ok=True)                                                                                                                                                                        


        if files_to_change is not None:
            print("files_to_change:", files_to_change)
            for file_to_change in files_to_change:
                host_file = list( file_to_change.keys() )[0]
                target_path = list( file_to_change.values() )[0]
                self.copy_to_container(Path(host_file), Path(target_path))

        if init_patch is not None:
            print("init_patch:", init_patch)
            for i_patch in init_patch:
                self.apply_patch(i_patch)

    def apply_patch(self, patch_file: Path):
        patch_file_container = f"{self.config.cwd}/patch.diff"
        print("Patch file in container:", patch_file_container)
        self.copy_to_container(patch_file, PurePosixPath(patch_file_container))

        print(self.execute(f"ls {self.config.cwd}"))

        applied_patch = False

        GIT_APPLY_CMDS = [
            "git apply --verbose",
            "git apply --verbose --reject",
            "patch --batch --fuzz=5 -p1 -i",
        ]

        result_messages = {}

        for git_apply_cmd in GIT_APPLY_CMDS:
            val = self.execute(
                f"{git_apply_cmd} {patch_file_container}",
            )

            print(git_apply_cmd, val)

            if val['returncode'] == 0:
                applied_patch = True
                self.execute(f"rm -rf {patch_file_container}")
                return

            result_messages[git_apply_cmd] = val['output']

        if not applied_patch:
            error_file = patch_file.with_name(patch_file.name + ".error2")
            error_file.write_text(json.dumps(result_messages))
            raise PatchApplyError()

    def get_template_vars(self) -> dict[str, Any]:
        return asdict(self.config)

    def execute(self, command: str, cwd: str = "", timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in a Singularity container and return the result as a dict."""
        # Remove INFO blocks caused by --fakeroot in env where suid mapping is missing (generally on all HPCs)
        # -q flag is quite execution which will supress all INFO blocks but all the warning & errors will propagate
        # -s flag is silent execution which will supress INFO, WARNING and ERRORS
        cmd = [self.config.executable,"-s", "exec", "--fakeroot"]

        # Do not inherit directories and env vars from host
        cmd.extend(["--contain", "--cleanenv"])

        if self.runtime_config.get("need_gpu", False):
            cmd.append("--nv")

        work_dir = cwd or self.config.cwd # config.cwd = /testbed
        
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

        if self.multilingual:
            bind = f"{str(self.sandbox_dir)}/root:/root"
            cmd.extend(["--bind", bind])
            bind = f"{str(self.sandbox_dir)}/tmp:/tmp"
            cmd.extend(["--bind", bind])

        # Load the conda file and activate the env
        # --cleanenv will flush out conda variables so need to call conda.sh to set them again
        if self.multilingual:
            wrapped = command
        else:
            prolog = "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed"
            wrapped = f"{prolog} && {command}"
        
        # cmd.extend(["--writable", str(self.sandbox_dir), "bash", "-c", command])
        cmd.extend(["--writable", str(self.sandbox_dir), "bash", "-c", wrapped])

        # For normal exec use the config.timeout but during eval use the timeout provided in the args
        timeout = self.config.timeout if timeout is None else timeout
        result = subprocess.run(
            cmd,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        print("CMD:", cmd)


        return {"output": result.stdout, "returncode": result.returncode}

    def copy_to_container(self, src: Path , dst: Path | PurePosixPath):
        """
            Copy a file from local to a docker container

            Args:
                src (Path): Source file path
                dst (Path): Destination file path in the container
        """
        if not dst.parent or dst.parent == Path():
            raise ValueError(
                f"Destination path parent directory cannot be empty!, dst: {dst}"
            )

        tmp_bind = "/tmp/tmpfile.py"
        cmd = [self.config.executable,
                "-q",
                "exec",
                "--fakeroot",
                "--writable",
                f"--bind={src}:{tmp_bind}",
                self.sandbox_dir,
                "bash",
                "-lc",
                f"cp '{tmp_bind}' '{dst}'"
        ]
        subprocess.run(cmd, check=True)

    def exec_run_with_timeout(self, cmd, timeout: int | None = 60):
        """
            Run a command in a container with a timeout.

            Args:
                cmd (str): Command to run.
                timeout (int): Timeout in seconds.
        """
        start_time = time.time()
        timed_out = False
        output = ""
        returncode = 1

        try:
            result = self.execute( cmd, timeout=timeout)
            output = result["output"]
            returncode = result["returncode"]

        except subprocess.TimeoutExpired as e:
            timed_out = True
            output = e.stdout if e.stdout else ""
            # CPython quirk: even with text=True, TimeoutExpired.stdout carries the
            # raw (undecoded) bytes captured before the kill. Decode so callers that
            # do `str in output` (e.g. _run_overlay's JUnit-marker split) don't crash.
            if isinstance(output, (bytes, bytearray)):
                output = output.decode("utf-8", errors="replace")
            returncode = -1  # distinguish timeout

        end_time = time.time()
        return output, timed_out, end_time - start_time, returncode

    def _chmod_u_rwX(self, p: Path) -> None:
        """Best-effort: make path user-writable; add +x for dirs."""
        try:
            mode = stat.S_IMODE(p.lstat().st_mode)
            if p.is_dir():
                os.chmod(p, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            else:
                os.chmod(p, mode | stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass

    def _chmod_leftovers(self, root: Path) -> None:
        """
        After a failed delete, fix perms on *remaining* paths.
        Critical: also chmod parents of leftovers, since parent non-writable blocks deletion.
        """
        try:
            # Deepest-first so we don't lose traversal
            paths = sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True)
            for p in paths:
                self._chmod_u_rwX(p)
                self._chmod_u_rwX(p.parent)  # key for your 'parent is 0555' case
            self._chmod_u_rwX(root)
        except Exception:
            pass

    def cleanup(self):
        if self.sandbox_dir.exists():
            try:
                shutil.rmtree(self.sandbox_dir)
                return
            except Exception:
                pass

            self._chmod_leftovers(self.sandbox_dir)
            try:
                shutil.rmtree(self.sandbox_dir)
                return
            except Exception:
                pass

            try:
                root = self.sandbox_dir
                quarantine_base = Path(os.getenv("QUARANTINE_BASE"))
                dest = quarantine_base / f"{root.name}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
                shutil.move(str(root), str(dest))
            except Exception:
                self.logger.warning("Failed to move or delete: %s", self.sandbox_dir)



    def __del__(self):
        """Cleanup sandbox when object is destroyed."""
        self.cleanup()

