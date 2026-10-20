#!/usr/bin/env bash
# Run one test state for a Go repo.
#   run_state_go.sh <checkout_sha> <overlay_sha|-> <junit_out> <test_file> [test_file...]
set -uo pipefail

SHA="$1"; OVERLAY="$2"; JUNIT="$3"; shift 3
TESTS=("$@")
REPO_DIR="${REPO_DIR:-/testbed}"
INSTALL_DIR="${INSTALL_DIR:-/opt/install}"
cd "$REPO_DIR"

git checkout -f "$SHA"
# -fd keeps vendor/
git clean -fdq
if [ -f .gitmodules ]; then
  git submodule update --init --recursive 2>/dev/null || true
fi
if [ "$OVERLAY" != "-" ]; then
  # overlay one file at a time
  for _f in "${TESTS[@]}"; do
    if git checkout "$OVERLAY" -- "$_f" 2>/dev/null; then continue; fi
    # deleted at <sha>: remove stale copy
    if [ -e "$_f" ]; then
      rm -rf "$_f"
      echo "overlay: $_f deleted at ${OVERLAY:0:12} -> removed stale copy"
    else
      echo "overlay: $_f not present at ${OVERLAY:0:12} (skipped)"
    fi
  done
fi

# --- install: recipe of the image's commit
RECIPE_SHA="${FORESIGHT_RECIPE_SHA:-$SHA}"
RECIPE="$INSTALL_DIR/$(echo "$RECIPE_SHA" | cut -c1-12).json"
if [ -f "$RECIPE" ]; then
  echo "run_state_go: using agent recipe $RECIPE"
  python3 - "$RECIPE" >/tmp/_install.sh <<'PYEOF'
import json, sys
print("set -euo pipefail")
for cmd in json.load(open(sys.argv[1])).get("install", []):
    print(cmd)
PYEOF
  bash /tmp/_install.sh || { echo "run_state_go: INSTALL FAILED"; exit 3; }
else
  # no recipe: go mod download
  echo "run_state_go: no recipe for $(echo "$RECIPE_SHA" | cut -c1-12); using go mod download"
  go mod download all 2>/dev/null || echo "run_state_go: download failed (continuing)"
fi

exec /usr/local/bin/go_run_tests.sh "$JUNIT" "${TESTS[@]}"
