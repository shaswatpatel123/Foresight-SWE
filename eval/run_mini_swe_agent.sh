#!/bin/bash
# Foresight eval with mini-swe-agent on a vLLM-served model.
# Edit the variables below, then run: bash eval/run_mini_swe_agent.sh
set -euo pipefail

# ---- SET THESE ----
export QUARANTINE_BASE=/path/to/quarantine_dir          # scratch dir for sandboxes
DATASET=/path/to/foresight_dataset.jsonl
CONFIG=minisweagent/src/minisweagent/config/extra/foresight_glm53flash.yaml   # set api_base inside
OUT=eval_runs/glm53flash
WORKERS=4
# -------------------

export FORESIGHT_REUSE_ENV=1                            # use each image's prebuilt env
export MSWEA_SINGULARITY_EXECUTABLE=$(command -v apptainer)

# 1. agent run -> $OUT/preds.json
mini-extra foresight --subset $DATASET --config $CONFIG \
    --environment-class foresightSingularity --workers $WORKERS -o $OUT

# 2. grading -> $OUT/final_foresight_report.json
mini-extra foresight --subset $DATASET --config $CONFIG \
    --environment-class foresightSingularity --workers $WORKERS -o $OUT --run-only-eval
