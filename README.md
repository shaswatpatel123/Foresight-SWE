<div align="center">

# Do Coding Agents Catch Their Own Bugs? (They Don't.)

[![arXiv](https://img.shields.io/badge/arXiv-2611.01181-b31b1b.svg)](https://arxiv.org/abs/2611.01181)
[![Dataset](https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-Foresight--SWE-yellow)](https://huggingface.co/datasets/shaswat123/Foresight-SWE)
[![Downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fhuggingface.co%2Fapi%2Fdatasets%2Fshaswat123%2FForesight-SWE&query=%24.downloads&label=downloads&color=yellow)](https://huggingface.co/datasets/shaswat123/Foresight-SWE)
[![Leaderboard](https://img.shields.io/badge/Leaderboard-website-blue)](https://shaswatpatel123.github.io/Foresight-SWE/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

In software development, engineers need to foresee and fix potential problems with their code that are not explicitly specified in requirements, such as pre-emptively considering vulnerabilities or other bugs from a code change.
But current benchmarks often do not measure these future-looking consequences.
We introduce **Foresight-SWE**, a benchmark for measuring whether coding agents can anticipate and address unstated problems during software engineering tasks.
Foresight-SWE consists of 212 samples spanning 4 languages and 35 popular open-source repositories.
Each sample contains two pull requests (PRs), the second of which resolves defects the first one introduced.
To measure whether agents exhibit foresight, we define the **foresight resolve rate (FRR)** as the fraction of instances where an agent fully resolves the original task while also avoiding the defect identified by a later PR.
We evaluate eleven large language models and find that FRR ranges from only 6.13% to 24.53%, indicating that models largely fail to address problems introduced by the first PR change.
This raises important questions about how models are trained and aligned in coding contexts, as well as how code review tools should be improved for more fail-safe coding.
Foresight-SWE shifts evaluations in this direction.

## Setup

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and either Apptainer/Singularity or Docker.

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ./minisweagent     # eval harness (mini-extra)
uv pip install -e ".[api,llm]"       # mining / pipeline code (foresight)
```

Download the dataset from [Hugging Face](https://huggingface.co/datasets/shaswat123/Foresight-SWE) (`data/verified.jsonl`) and set `DATASET` in the eval script to its path.
Images are pulled from [`docker.io/shaswatpatel123/foresight-swe`](https://hub.docker.com/r/shaswatpatel123/foresight-swe); each instance names its own image in the `docker_image` column.

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

## Citation

```bibtex
@misc{patel2026codingagentscatchbugs,
      title={Do Coding Agents Catch Their Own Bugs? (They Don't.)}, 
      author={Shaswat Patel and Michel Liao and Peter Henderson and He He and Valerie Chen},
      year={2026},
      eprint={2611.01181},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2611.01181}, 
}
```
