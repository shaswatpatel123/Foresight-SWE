<div align="center">

# foresightSWE

**Do Coding Agents Catch Their Own Bugs? (They Don't)**

[![arXiv](https://img.shields.io/badge/arXiv-XXXX.XXXXX-b31b1b.svg)](https://arxiv.org/abs/XXXX.XXXXX)
[![Dataset](https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-foresightSWE-yellow)](https://huggingface.co/datasets/HF_USER/HF_DATASET)
[![Downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fhuggingface.co%2Fapi%2Fdatasets%2FHF_USER%2FHF_DATASET&query=%24.downloads&label=downloads&color=yellow)](https://huggingface.co/datasets/HF_USER/HF_DATASET)
[![Leaderboard](https://img.shields.io/badge/Leaderboard-website-blue)](https://LEADERBOARD_URL)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

## Setup

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and either Apptainer/Singularity or Docker.

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ./minisweagent     # eval harness (mini-extra)
uv pip install -e ".[api,llm]"       # mining / pipeline code (foresight)
```

Download the dataset from <hf-dataset>. Images are pulled from `docker.io/shaswatpatel123/foresight`.

For a vLLM-served model, edit its config in `minisweagent/src/minisweagent/config/extra/`:
set `api_base: http://<your-vllm-host>:<port>/v1` and point `litellm_model_registry` at `minisweagent/model_registry.json`.

## Run

Pick a script, edit the `SET THESE` block at the top, and run it from the repo root:

| Agent | Script |
|---|---|
| mini-swe-agent (vLLM model) | `bash eval/run_mini_swe_agent.sh` |
| Claude Code | `bash eval/run_claude_code.sh` |
| Codex | `bash eval/run_codex.sh` |

Each script runs the agent (writing `preds.json`), then grades it (writing `final_foresight_report.json`).

- **Gold sanity check:** `--gold` writes the gold PR1 patches as predictions (no model). Grading should give PR1 ≈ 100% and anticipation ≈ 0%.
- **Docker instead of Apptainer:** `--environment-class foresightDocker`.
- **Native CLI agents:** `--agent-backend claude_code` or `--agent-backend codex`, with the matching `foresight_claude_*` / `foresight_codex*` config.
- **PR2-direct (oracle):** `--task-column pr2_problem_statement --start-commit-field pr2_base_sha --image-field pr2base_image --grade-mode pr2`.
