#!/usr/bin/env bash
# Run one test state for a Python repo.
#   run_state.sh <checkout_sha> <overlay_sha|-> <junit_out> <test_file> [test_file...]
# Fallback env: EXTRA_DEPS, BUILD_DEPS, NO_BUILD_ISOLATION, EDITABLE, ADDOPTS_CLEAR
set -euo pipefail

SHA="$1"; OVERLAY="$2"; JUNIT="$3"; shift 3
TESTS=("$@")
REPO_DIR="${REPO_DIR:-/testbed}"
cd "$REPO_DIR"

git checkout -f "$SHA"
# full clean unless reusing a prebuilt env
if [ "${FORESIGHT_REUSE_ENV:-0}" = "1" ] && { [ -x /opt/venv/bin/python ] || [ -d node_modules ]; }; then
  git clean -fdq          # keep prebuilt env
else
  git clean -fdxq
fi
# sync submodules to this commit
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

# --- install: recipe of the image's commit, else ENV
# never prompt
export CI=1 CONTINUOUS_INTEGRATION=1 npm_config_yes=true DEBIAN_FRONTEND=noninteractive
INSTALL_DIR="${INSTALL_DIR:-/opt/install}"
RECIPE_SHA="${FORESIGHT_RECIPE_SHA:-$SHA}"
RECIPE="$INSTALL_DIR/$(echo "$RECIPE_SHA" | cut -c1-12).json"
[ -f "$RECIPE" ] || RECIPE=""

# reinstall if the recipe builds or writes test files
_recipe_needs_install() {
  [ -n "${RECIPE:-}" ] && [ -f "$RECIPE" ] || return 1
  python3 - "$RECIPE" <<'PYEOF'
import json, re, sys
c = json.load(open(sys.argv[1]))
ins = " ".join(c.get("install", []) or [])
tc = c.get("test_cmd") or ""
if not isinstance(tc, str): tc = ""
BUILD = ("run build", "build:ci", "pnpm build", "npm run build", "tsc -b",
         "turbo build", "setup.py build", "meson compile")
if any(b in ins for b in BUILD):
    sys.exit(0)
for m in re.finditer(r">\s*(/[^\s;&|]+)", ins):      # install writes a path
    if m.group(1) in tc:                              # that test_cmd uses
        sys.exit(0)
sys.exit(1)
PYEOF
}

if [ "${FORESIGHT_REUSE_ENV:-0}" = "1" ] && [ -x /opt/venv/bin/python ] && ! _recipe_needs_install; then
  echo "run_state: reusing prebuilt /opt/venv (no install)"
  PY=/opt/venv/bin/python
elif [ -n "$RECIPE" ]; then
  echo "run_state: using agent recipe $RECIPE"
  python3 - "$RECIPE" "$PWD/.venv" >/tmp/_install.sh <<'PYEOF'
import json, sys
recipe, venv = sys.argv[1], sys.argv[2]
cfg = json.load(open(recipe))
print("set -euo pipefail")
for cmd in cfg.get("install", []):
    # /opt/venv -> this .venv
    print(cmd.replace("/opt/venv", venv))
PYEOF
  # drop any old .venv
  rm -rf "$PWD/.venv"
  # fail loudly on install error
  bash /tmp/_install.sh || { echo "run_state: INSTALL FAILED"; exit 3; }
  PY="$PWD/.venv/bin/python"
else
  echo "run_state: no recipe for ${SHA:0:12}, falling back to baked ENV deps"
  # --clear: an old .venv may remain
  uv venv --clear --python "${PYVER:-3.12}" .venv
  PY=.venv/bin/python
  # build deps first
  if [ -n "${BUILD_DEPS:-}" ]; then
    # shellcheck disable=SC2086
    uv pip install --python "$PY" ${BUILD_DEPS}
  fi
  NBI=; [ "${NO_BUILD_ISOLATION:-0}" = "1" ] && NBI="--no-build-isolation"
  E="-e"; [ "${EDITABLE:-1}" = "0" ] && E=""
  # shellcheck disable=SC2086
  uv pip install --python "$PY" $NBI $E . ${EXTRA_DEPS:-}
fi

ADDOPTS=()
[ "${ADDOPTS_CLEAR:-0}" = "1" ] && ADDOPTS=(-o addopts=)
# pass only existing .py files to pytest
RUN_TESTS=()
for _t in "${TESTS[@]}"; do
  case "$_t" in *.py) ;; *) continue;; esac
  # skip files deleted by the overlay
  [ -e "$_t" ] || { echo "run_state: $_t absent after overlay -> not passed to pytest"; continue; }
  RUN_TESTS+=("$_t")
done
if [ ${#RUN_TESTS[@]} -eq 0 ]; then
  echo "run_state: no .py among the test files; nothing to run"; exit 6
fi
[ ${#RUN_TESTS[@]} -ne ${#TESTS[@]} ] && \
  echo "run_state: overlaid ${#TESTS[@]} file(s), running ${#RUN_TESTS[@]} .py file(s)"
"$PY" -m pytest "${RUN_TESTS[@]}" \
  --junitxml="$JUNIT" -p no:cacheprovider -q --continue-on-collection-errors "${ADDOPTS[@]}"
