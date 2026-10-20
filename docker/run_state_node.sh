#!/usr/bin/env bash
# Run one test state for a JS/TS repo.
#   run_state_node.sh <checkout_sha> <overlay_sha|-> <junit_out> <test_file> [test_file...]
set -uo pipefail

SHA="$1"; OVERLAY="$2"; JUNIT="$3"; shift 3
TESTS=("$@")
REPO_DIR="${REPO_DIR:-/testbed}"
INSTALL_DIR="${INSTALL_DIR:-/opt/install}"
cd "$REPO_DIR"

# resolve recipe before cleaning
RECIPE_SHA="${FORESIGHT_RECIPE_SHA:-$SHA}"
RECIPE="$INSTALL_DIR/$(echo "$RECIPE_SHA" | cut -c1-12).json"

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


git checkout -f "$SHA"
# -fd keeps node_modules
if _recipe_needs_install; then
  git clean -fdxq   # reinstalling: full clean
else
  git clean -fdq    # reusing node_modules
fi
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
# never prompt
export CI=1 CONTINUOUS_INTEGRATION=1 npm_config_yes=true DEBIAN_FRONTEND=noninteractive
# reuse the prebuilt env if allowed


if [ "${FORESIGHT_REUSE_ENV:-0}" = "1" ] && [ -d node_modules ] && ! _recipe_needs_install; then
  echo "run_state_node: reusing prebuilt node_modules ($(ls node_modules | wc -l) pkgs, no install)"
elif [ -f "$RECIPE" ]; then
  echo "run_state_node: using agent recipe $RECIPE"
  python3 - "$RECIPE" >/tmp/_install.sh <<'PYEOF'
import json, sys
print("set -euo pipefail")
for cmd in json.load(open(sys.argv[1])).get("install", []):
    print(cmd)
PYEOF
  bash /tmp/_install.sh || { echo "run_state_node: INSTALL FAILED"; exit 3; }
else
  # no recipe: fail, don't guess
  echo "run_state_node: NO RECIPE for $(echo "$RECIPE_SHA" | cut -c1-12)"; exit 3
fi

# --- run tests
exec /usr/local/bin/node_run_tests.sh "$JUNIT" "${TESTS[@]}"
