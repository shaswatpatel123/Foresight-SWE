#!/bin/bash
# Foresight eval with the Codex CLI as the agent.
# Run `codex login` first (creates ~/.codex/auth.json).
# Edit the variables below, then run: bash eval/run_codex.sh
set -euo pipefail

# ---- SET THESE ----
export QUARANTINE_BASE=/path/to/quarantine_dir          # scratch dir for sandboxes
DATASET=/path/to/foresight_dataset.jsonl
CONFIG=minisweagent/src/minisweagent/config/extra/foresight_codex56sol.yaml
OUT=eval_runs/codex56sol
WORKERS=8
# -------------------

export FORESIGHT_REUSE_ENV=1                            # use each image's prebuilt env
export MSWEA_SINGULARITY_EXECUTABLE=$(command -v apptainer)

# 1. agent run -> $OUT/preds.json
mini-extra foresight --subset $DATASET --config $CONFIG --agent-backend codex \
    --environment-class foresightSingularity --workers $WORKERS -o $OUT

# 2. grading -> $OUT/final_foresight_report.json
mini-extra foresight --subset $DATASET --config $CONFIG \
    --environment-class foresightSingularity --workers $WORKERS -o $OUT --run-only-eval
