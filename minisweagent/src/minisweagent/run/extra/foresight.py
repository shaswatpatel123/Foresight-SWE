#!/usr/bin/env python3
"""Run mini-SWE-agent on the Foresight benchmark."""

import concurrent.futures
import copy
import json
import os
import random
import re
import threading
import time
import traceback
from pathlib import Path

import typer
import yaml
from rich.live import Live

from minisweagent import Environment
from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.cli_agent import CLIAgent, USAGE_LIMIT_HIT
from minisweagent.config import builtin_config_dir, get_config_path
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.extra.utils.batch_progress import RunBatchProgressManager
from minisweagent.run.utils.save import save_traj
from minisweagent.utils.log import add_file_handler, logger

from minisweagent.evaluation.foresight import evaluate_foresight, reanticipate_only, make_foresight_report

_HELP_TEXT = "Run mini-SWE-agent on the foresight anticipation-gap benchmark (batch mode)."

DEFAULT_DATASET = os.getenv("FORESIGHT_DATASET")

app = typer.Typer(rich_markup_mode="rich", add_completion=False)

DATASET_MAPPING = {
    "foresight": DEFAULT_DATASET,
}

# Dep pins for shared per-repo images.
_REPO_DEP_PINS: dict[str, dict[str, str]] = {
    "pandas": {
        # cython 3.2.6 builds; 3.2.5 fails
        "BUILD_DEPS": "meson-python meson ninja cython==3.2.6 numpy==2.4.6 wheel versioneer",
        "EXTRA_DEPS": "pytest hypothesis pytest-xdist tzdata numpy==2.4.6",
    },
    "xarray": {
        "EXTRA_DEPS": "pytest numpy==2.4.6 pandas==3.0.3",
    },
    "networkx": {
        "EXTRA_DEPS": "pytest numpy==2.4.6",
    },
}

# JSON-string fields decoded to lists.
_LIST_FIELDS = [
    "pr1_test_files", "anticipation_test_files",
    "FAIL_TO_PASS", "PASS_TO_PASS", "ANTICIPATION_FAIL_TO_PASS",
    "PR2_FAIL_TO_PASS", "PR2_PASS_TO_PASS",
]

_OUTPUT_FILE_LOCK = threading.Lock()


class ProgressTrackingAgent(DefaultAgent):
    """DefaultAgent that reports per-step progress."""

    def __init__(self, *args, progress_manager: RunBatchProgressManager, instance_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_manager = progress_manager
        self.instance_id = instance_id

    def step(self) -> dict:
        self.progress_manager.update_instance_status(
            self.instance_id, f"Step {self.model.n_calls + 1:3d} (${self.model.cost:.2f})"
        )
        return super().step()


def load_instances(dataset_path: str) -> list[dict]:
    """Load instances from JSONL (mixed-type columns)."""
    with open(dataset_path) as f:
        return [json.loads(line) for line in f if line.strip()]


def normalize_instances(instances: list[dict]) -> list[dict]:
    """Decode JSON-string list fields into real lists (idempotent)."""
    for inst in instances:
        for f in _LIST_FIELDS:
            v = inst.get(f)
            if isinstance(v, str):
                s = v.strip()
                inst[f] = json.loads(s) if s.startswith("[") else ([v] if v else [])
            elif v is None:
                inst[f] = []
    return instances


# Repos given a private network (none).
_NET_ISOLATE_REPOS: set[str] = set()

# Apptainer's /etc/localtime bind corrupts zoneinfo.
_NO_LOCALTIME_REPOS = {"pandas"}


# Instances whose prebuilt env is stale.
_NO_REUSE_INSTANCES = {"axios__10833-11029"}

# AVX-512 sort breaks tie-order tests.
_NO_AVX512_INSTANCES = {"pandas__44267-46798"}
_AVX512_FEATURES = ("AVX512F AVX512CD AVX512_SKX AVX512_CLX AVX512_CNL AVX512_ICL "
                    "AVX512_KNL AVX512_KNM AVX512_SPR")


def get_foresight_image(instance: dict, path_local_images: Path | None,
                        image_field: str = "docker_image") -> str | Path:
    """SIF path for an instance, else docker://."""
    image = instance.get(image_field) or ""
    if not image:
        raise KeyError(
            f"{instance['instance_id']}: no '{image_field}' in the row."
        )
    if path_local_images is not None:
        tag = str(image).rsplit(":", 1)[-1]
        if tag and (local := path_local_images / f"{tag}.sif").is_file():
            return local
    return "docker://" + image


def get_foresight_environment(config: dict, instance: dict, path_local_images: Path | None,
                              skip_git_reset: bool, init_patch: list | None = None,
                              start_commit_field: str = "base_commit",
                              image_field: str = "docker_image") -> Environment:
    # copy: config is shared across workers
    env_config = copy.deepcopy(config.get("environment", {}))
    env_config.setdefault("environment_class", "foresightSingularity")
    env_config["image"] = get_foresight_image(instance, path_local_images, image_field)
    # Pins only for non-inst-* images.
    is_per_instance = str(instance.get(image_field, "")).rsplit(":", 1)[-1].startswith("inst-")
    if not is_per_instance and (pins := _REPO_DEP_PINS.get(instance["instance_id"].split("__")[0])):
        env_config["env"] = {**env_config.get("env", {}), **pins}
    env_config["net_isolate"] = instance["instance_id"].split("__")[0] in _NET_ISOLATE_REPOS
    env_config["no_localtime"] = instance["instance_id"].split("__")[0] in _NO_LOCALTIME_REPOS
    if instance["instance_id"] in _NO_REUSE_INSTANCES:
        # overrides global FORESIGHT_REUSE_ENV
        env_config["env"] = {**env_config.get("env", {}), "FORESIGHT_REUSE_ENV": "0"}
    if instance["instance_id"] in _NO_AVX512_INSTANCES:
        env_config["env"] = {**env_config.get("env", {}),
                             "NPY_DISABLE_CPU_FEATURES": _AVX512_FEATURES}
    # agent start commit
    env_config["base_commit"] = instance[start_commit_field]
    # commit message for codereview mode
    env_config["codereview_commit_msg"] = instance.get("problem_statement", "")
    env_config["skip_git_reset"] = skip_git_reset
    env_config["files_to_change"] = None
    # round-1 patch, applied before .git strip
    env_config["init_patch"] = init_patch
    return get_environment(env_config, default_type="foresightSingularity")


def update_preds_file(output_path: Path, instance_id: str, model_name: str, result: str):
    with _OUTPUT_FILE_LOCK:
        data = json.loads(output_path.read_text()) if output_path.exists() else {}
        data[instance_id] = {
            "model_name_or_path": model_name,
            "instance_id": instance_id,
            "model_patch": result,
        }
        output_path.write_text(json.dumps(data, indent=2))


def remove_from_preds_file(output_path: Path, instance_id: str):
    if not output_path.exists():
        return
    with _OUTPUT_FILE_LOCK:
        data = json.loads(output_path.read_text())
        if instance_id in data:
            del data[instance_id]
            output_path.write_text(json.dumps(data, indent=2))


def get_from_preds_file(output_path: Path, instance_id: str):
    if not output_path.exists():
        raise FileNotFoundError(f"output_path does not exist - {output_path}")
    with _OUTPUT_FILE_LOCK:
        data = json.loads(output_path.read_text())
        entry = data.get(instance_id)
        return entry["model_patch"] if entry else None


def _run_continue(agent: "ProgressTrackingAgent", task: str, resume_messages: list) -> tuple[str, str]:
    """Resume round-1 messages, then add task."""
    from minisweagent.agents.default import NonTerminatingException, TerminatingException
    agent.extra_template_vars |= {"task": task}
    agent.messages = list(resume_messages)
    agent.add_message("user", task)
    while True:
        try:
            agent.step()
        except NonTerminatingException as e:
            agent.add_message("user", str(e))
        except TerminatingException as e:
            agent.add_message("user", str(e))
            return type(e).__name__, str(e)


def process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager: RunBatchProgressManager,
    run_only_eval: bool,
    path_local_images: Path | None,
    task_column_name: str = "problem_statement",
    init_patch_map: dict | None = None,
    task_suffix: str = "",
    resume_traj_dir: Path | None = None,
    agent_backend: str = "mini",
    # keep new params after agent_backend
    resume_session_map: dict | None = None,
    start_commit_field: str = "base_commit",
    image_field: str = "docker_image",
    grade_mode: str = "pr1",
    heal_ant_only: bool = False,
) -> None:
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    patch_file = instance_dir / "patch.diff"
    instance_dir.mkdir(parents=True, exist_ok=True)

    if not run_only_eval:
        remove_from_preds_file(output_dir / "preds.json", instance_id)
        (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

    # Round-2: seed the round-1 patch as init_patch (applied on top of base_commit).
    init_patch = None
    if init_patch_map and instance_id in init_patch_map:
        init_patch = [Path(init_patch_map[instance_id])]
    elif init_patch_map:
        # round-2 requested but this instance has no round-1 patch -> nothing to build on
        logger.warning(f"{instance_id}: not in init_patch_map (empty round-1 patch?), skipping")
        progress_manager.on_instance_end(instance_id, "no_round1_patch")
        return

    # usage limit hit: skip remaining instances
    if not run_only_eval and USAGE_LIMIT_HIT.is_set():
        progress_manager.on_instance_start(instance_id)
        progress_manager.on_instance_end(instance_id, "usage_limit_reached")
        return

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "starting container")

    agent = None
    model = None
    env = None
    extra_info = None
    exit_status = "None"
    result = ""

    try:
        # Agent phase strips .git (skip_git_reset=False); grade phase keeps it (True).
        env = get_foresight_environment(config, instance, path_local_images,
                                        skip_git_reset=run_only_eval, init_patch=init_patch,
                                        start_commit_field=start_commit_field,
                                        image_field=image_field)

        if not run_only_eval:
            if agent_backend == "mini":
                model = get_model(config=config.get("model", {}))
                agent = ProgressTrackingAgent(
                    model, env,
                    progress_manager=progress_manager,
                    instance_id=instance_id,
                    **config.get("agent", {}),
                )
            else:
                # native CLI backend (claude_code/codex)
                agent_cfg = {k: v for k, v in config.get("agent", {}).items() if k != "backend"}
                # resume saved session if present
                if resume_session_map and instance_id in resume_session_map:
                    agent_cfg["resume_session_file"] = resume_session_map[instance_id]
                elif resume_session_map:
                    logger.warning(f"{instance_id}: not in resume_session_map; starting cold")
                agent = CLIAgent(
                    env,
                    backend=agent_backend,
                    progress_manager=progress_manager,
                    instance_id=instance_id,
                    artifact_dir=instance_dir,
                    **agent_cfg,
                )
                model = agent.model
            task = instance[task_column_name] + task_suffix
            if resume_traj_dir is not None:
                # Continue-mode: resume round-1 messages, then append the edge-case task.
                traj_file = resume_traj_dir / instance_id / f"{instance_id}.traj.json"
                resume_msgs = json.loads(traj_file.read_text()).get("messages", [])
                exit_status, result = _run_continue(agent, task, resume_msgs)
            else:
                exit_status, result = agent.run(task)
        else:
            result = get_from_preds_file(output_dir / "preds.json", instance_id)
            if result is None:
                logger.warning(f"{instance_id}: no prediction in preds.json")
                progress_manager.on_instance_end(instance_id, "no_prediction")
                return
            if len(result.strip()) == 0:
                # empty prediction: mark EMPTY_PATCH
                exit_status = "EMPTY_PATCH"
            else:
                progress_manager.update_instance_status(instance_id, "applying patch")
                patch_file.write_text(result)
                env.apply_patch(patch_file)

        if run_only_eval and len(result.strip()) > 0:
            if heal_ant_only:
                progress_manager.update_instance_status(instance_id, "healing (anticipation only)")
                exit_status = reanticipate_only(instance, instance_dir, env)
            else:
                progress_manager.update_instance_status(instance_id, "grading (PR1 + anticipation)")
                exit_status = evaluate_foresight(instance, instance_dir, env,
                                                 grade_mode=grade_mode)

    except Exception as e:
        print(f"Error processing instance {instance_id}: {e}")
        traceback.print_exc()
        exit_status, result = type(e).__name__, str(e)
        extra_info = {"traceback": traceback.format_exc()}
    finally:
        if not run_only_eval:
            save_traj(
                agent,
                instance_dir / f"{instance_id}.traj.json",
                exit_status=exit_status,
                result=result,
                extra_info=extra_info,
                instance_id=instance_id,
                print_fct=logger.info,
            )
            model_name = model.config.model_name if model is not None else "unknown"
            # skip save so re-run retries it
            if exit_status != "usage_limit_reached":
                update_preds_file(output_dir / "preds.json", instance_id, model_name, result)
        # always tear down the sandbox
        if env is not None:
            try:
                env.cleanup()
            except Exception:
                pass
        progress_manager.on_instance_end(instance_id, exit_status)


def filter_instances(instances: list[dict], *, filter_spec: str, filter_ids: str,
                     slice_spec: str = "", shuffle: bool = False) -> list[dict]:
    if shuffle:
        instances = sorted(instances.copy(), key=lambda x: x["instance_id"])
        random.seed(42)
        random.shuffle(instances)
    before = len(instances)
    if filter_spec:
        instances = [i for i in instances if re.match(filter_spec, i["instance_id"])]
    if filter_ids:
        ids = set(filter_ids.split("|"))
        instances = [i for i in instances if i["instance_id"] in ids]
    if (after := len(instances)) != before:
        logger.info(f"Instance filter: {before} -> {after} instances")
    if slice_spec:
        values = [int(x) if x else None for x in slice_spec.split(":")]
        instances = instances[slice(*values)]
    return instances


def handle_gold_test(instances: list[dict], output_path: Path, gold_patch_field: str = "patch") -> None:
    """Write gold patches as predictions."""
    write_dict = {}
    for inst in instances:
        write_dict[inst["instance_id"]] = {
            "model_name_or_path": "gold",
            "instance_id": inst["instance_id"],
            "model_patch": inst[gold_patch_field],
        }
    (output_path / "preds.json").write_text(json.dumps(write_dict, indent=4))


# fmt: off
@app.command(help=_HELP_TEXT)
def main(
    subset: str = typer.Option("foresight", "--subset", help="Dataset name or path to a jsonl", rich_help_panel="Data selection"),
    split: str = typer.Option("train", "--split", help="Dataset split", rich_help_panel="Data selection"),
    slice_spec: str = typer.Option("", "--slice", help="Slice spec, e.g. '0:5'", rich_help_panel="Data selection"),
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex", rich_help_panel="Data selection"),
    filter_ids: str = typer.Option("", "--filter-ids", help="Filter by instance IDs (delimiter '|')", rich_help_panel="Data selection"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances", rich_help_panel="Data selection"),
    output: str = typer.Option("", "-o", "--output", help="Output directory", rich_help_panel="Basic"),
    workers: int = typer.Option(1, "-w", "--workers", help="Worker threads", rich_help_panel="Basic"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use", rich_help_panel="Basic"),
    model_class: str | None = typer.Option(None, "--model-class", help="Model class", rich_help_panel="Advanced"),
    agent_backend: str = typer.Option("mini", "--agent-backend", help="Phase-1 agent: 'mini' (default loop), 'claude_code', or 'codex' (native CLI, headless)", rich_help_panel="Basic"),
    redo_existing: bool = typer.Option(False, "--redo-existing", help="Redo existing instances", rich_help_panel="Data selection"),
    config_spec: Path = typer.Option(builtin_config_dir / "extra" / "foresight.yaml", "-c", "--config", help="Config file", rich_help_panel="Basic"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="Environment type", rich_help_panel="Advanced"),
    run_only_eval: bool = typer.Option(False, "--run-only-eval", help="Grade existing predictions only", rich_help_panel="Eval"),
    heal_ant_only: bool = typer.Option(False, "--heal-ant-only", help="Re-run only the anticipation tests and patch the report.", rich_help_panel="Eval"),
    generate_final_report: bool = typer.Option(False, "--generate-final-report", help="Aggregate reports only", rich_help_panel="Eval"),
    gold: bool = typer.Option(False, "--gold", help="Write gold patches as predictions (sanity)", rich_help_panel="Eval"),
    task_column: str = typer.Option("problem_statement", "--task-column", help="Dataset field used as the agent's task (e.g. pr2_problem_statement for the PR2-direct oracle)", rich_help_panel="Data selection"),
    start_commit_field: str = typer.Option("base_commit", "--start-commit-field", help="Dataset field for the agent's start commit (e.g. pr1_head_sha for PR2-direct)", rich_help_panel="Data selection"),
    gold_patch_field: str = typer.Option("patch", "--gold-patch-field", help="Dataset field written as the gold prediction under --gold (e.g. anticipation_patch for PR2-direct)", rich_help_panel="Eval"),
    image_field: str = typer.Option("docker_image", "--image-field", help="Dataset field holding the container image tag (e.g. pr2base_image for PR2-direct)", rich_help_panel="Data selection"),
    grade_mode: str = typer.Option("pr1", "--grade-mode", help="Which tests define success. 'pr1' (default): F2P=FAIL_TO_PASS, P2P=PASS_TO_PASS, plus ANT. 'pr2' (PR2-direct): F2P=ANT, P2P=PR2_PASS_TO_PASS off the same JUnit, PR1 overlay skipped (~2x faster)", rich_help_panel="Eval"),
    path_local_images: str | None = typer.Option(None, "--path-local-images", help="Dir of local SIF images", rich_help_panel="Basic"),
    init_patch_map: str | None = typer.Option(None, "--init-patch-map", help="Round-2: JSON {instance_id: round-1 patch path} seeded on top of base_commit", rich_help_panel="Round2"),
    edgecase_prompt_file: str | None = typer.Option(None, "--edgecase-prompt-file", help="Round-2: file whose text is appended to the problem statement (edge-case hunt prompt)", rich_help_panel="Round2"),
    resume_session_map: str | None = typer.Option(None, "--resume-session-map", help="Round-2 (claude_code): JSON {instance_id: saved .jsonl session} resumed via --resume; pair with --init-patch-map", rich_help_panel="Round2"),
    resume_traj_dir: str | None = typer.Option(None, "--resume-traj-dir", help="Round-2 continue-mode: dir of round-1 results; resumes <iid>/<iid>.traj.json messages", rich_help_panel="Round2"),
) -> None:
    # fmt: on
    if not os.getenv("QUARANTINE_BASE"):
        raise RuntimeError("Please setup a QUARANTINE_BASE using export QUARANTINE_BASE=<dir>")
    if subset == "foresight" and not DEFAULT_DATASET:
        raise RuntimeError("Please setup a FORESIGHT_DATASET using export FORESIGHT_DATASET=<path/to/foresight_dataset.jsonl>")
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Results will be saved to {output_path}")
    add_file_handler(output_path / "minisweagent.log")
    path_local_images = Path(path_local_images) if path_local_images is not None else None

    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info(f"Loading dataset {dataset_path}, split {split}...")
    instances = load_instances(dataset_path)
    instances = normalize_instances(instances)
    instances = filter_instances(instances, filter_spec=filter_spec, filter_ids=filter_ids,
                                 slice_spec=slice_spec, shuffle=shuffle)
    logger.info(f"Total instances: {len(instances)}")

    config = yaml.safe_load(get_config_path(config_spec).read_text())
    if environment_class is not None:
        config.setdefault("environment", {})["environment_class"] = environment_class
    if model is not None:
        config.setdefault("model", {})["model_name"] = model
    if model_class is not None:
        config.setdefault("model", {})["model_class"] = model_class

    # Round-2 elicited-anticipation inputs (all optional).
    init_patch_dict = json.loads(Path(init_patch_map).read_text()) if init_patch_map else None
    resume_session_dict = json.loads(Path(resume_session_map).read_text()) if resume_session_map else None
    task_suffix = ("\n\n" + Path(edgecase_prompt_file).read_text()) if edgecase_prompt_file else ""
    resume_dir = Path(resume_traj_dir) if resume_traj_dir else None
    if init_patch_dict is not None:
        logger.info(f"Round-2 mode: init_patch_map has {len(init_patch_dict)} entries"
                    f"{' (continue-mode)' if resume_dir else ' (fresh-mode)'}")

    if gold:
        logger.info(f"Writing gold predictions from '{gold_patch_field}'")
        handle_gold_test(instances, output_path, gold_patch_field)

    # report only, unless also grading
    if generate_final_report and not run_only_eval:
        predictions = json.loads((output_path / "preds.json").read_text())
        make_foresight_report(predictions, instances, output_path)
        return

    if not redo_existing and not run_only_eval and (output_path / "preds.json").exists():
        existing = set(json.loads((output_path / "preds.json").read_text()).keys())
        instances = [i for i in instances if i["instance_id"] not in existing]
        logger.info(f"Skipping {len(existing)} existing; running {len(instances)}")

    progress_manager = RunBatchProgressManager(len(instances), output_path / f"exit_statuses_{time.time()}.yaml")

    def process_futures(futures: dict):
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception as e:
                iid = futures[future]
                print(f"Error in future for {iid}: {e}")
                traceback.print_exc()
                progress_manager.on_uncaught_exception(iid, e)

    with Live(progress_manager.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_instance, inst, output_path, config, progress_manager,
                                run_only_eval, path_local_images, task_column,
                                init_patch_dict, task_suffix, resume_dir, agent_backend,
                                resume_session_map=resume_session_dict,
                                start_commit_field=start_commit_field,
                                image_field=image_field,
                                grade_mode=grade_mode,
                                heal_ant_only=heal_ant_only): inst["instance_id"]
                for inst in instances
            }
            try:
                process_futures(futures)
            except KeyboardInterrupt:
                logger.info("Cancelling pending jobs. Press ^C again to exit immediately.")
                for future in futures:
                    if not future.running() and not future.done():
                        future.cancel()
                process_futures(futures)

    predictions = json.loads((output_path / "preds.json").read_text())
    logger.info("Creating final report")
    make_foresight_report(predictions, instances, output_path)


if __name__ == "__main__":
    app()
