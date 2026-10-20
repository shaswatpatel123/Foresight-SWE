#!/bin/bash
# Foresight eval with the Claude Code CLI as the agent.
# Edit the variables below, then run: bash eval/run_claude_code.sh
set -euo pipefail

# ---- SET THESE ----
export QUARANTINE_BASE=/path/to/quarantine_dir          # scratch dir for sandboxes
export CLAUDE_CODE_OAUTH_TOKEN="<your-token>"            # from `claude setup-token`
DATASET=/path/to/foresight_dataset.jsonl
CONFIG=minisweagent/src/minisweagent/config/extra/foresight_claude_opus5.yaml
OUT=eval_runs/claude_opus5
WORKERS=8
# -------------------

export FORESIGHT_REUSE_ENV=1                            # use each image's prebuilt env
export MSWEA_SINGULARITY_EXECUTABLE=$(command -v apptainer)

# 1. agent run -> $OUT/preds.json
mini-extra foresight --subset $DATASET --config $CONFIG --agent-backend claude_code \
    --environment-class foresightSingularity --workers $WORKERS -o $OUT

# 2. grading -> $OUT/final_foresight_report.json
mini-extra foresight --subset $DATASET --config $CONFIG \
    --environment-class foresightSingularity --workers $WORKERS -o $OUT --run-only-eval
