#!/usr/bin/env bash
# Run one test state for Django (runtests.py).
#   run_state_django.sh <checkout_sha> <overlay_sha|-> <junit_out> <test_file> [test_file...]
set -euo pipefail

SHA="$1"; OVERLAY="$2"; JUNIT="$3"; shift 3
TESTS=("$@")
REPO_DIR="${REPO_DIR:-/testbed}"
SETTINGS="${DJANGO_SETTINGS:-test_sqlite}"
cd "$REPO_DIR"

git checkout -f "$SHA"
# keep the prebuilt env if reusing
if [ "${FORESIGHT_REUSE_ENV:-0}" = "1" ] && { [ -x /opt/venv/bin/python ] || [ -d node_modules ]; }; then
  git clean -fdq          # keep prebuilt env
else
  git clean -fdxq
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

# test paths -> runtests labels
LABELS=()
for t in "${TESTS[@]}"; do
  case "$t" in
    tests/*.py) ;;
    *) continue ;;
  esac
  l="${t#tests/}"; l="${l%/__init__.py}"; l="${l%.py}"; l="${l//\//.}"
  LABELS+=("$l")
done
# de-dup
IFS=$'\n' LABELS=($(printf '%s\n' "${LABELS[@]}" | sort -u)); unset IFS

# recipe of the image's commit; else `-e .`
INSTALL_DIR="${INSTALL_DIR:-/opt/install}"
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

if [ "${FORESIGHT_REUSE_ENV:-0}" = "1" ] && [ -x /opt/venv/bin/python ] && ! _recipe_needs_install; then
  echo "run_state_django: reusing prebuilt /opt/venv (no install)"
  PY=/opt/venv/bin/python
elif [ -f "$RECIPE" ]; then
  echo "run_state_django: using agent recipe $RECIPE"
  python3 - "$RECIPE" "$PWD/.venv" >/tmp/_install.sh <<'PYEOF'
import json, sys
recipe, venv = sys.argv[1], sys.argv[2]
for cmd in json.load(open(recipe)).get("install", []):
    print(cmd.replace("/opt/venv", venv))
PYEOF
  bash -euo pipefail /tmp/_install.sh
  PY="$PWD/.venv/bin/python"
else
  # --clear: an old .venv may remain
  uv venv --clear --python "${PYVER:-3.12}" .venv
  PY=.venv/bin/python
  uv pip install --python "$PY" -e .
fi

LOG="$(mktemp)"
set +e
"$PY" tests/runtests.py --settings="$SETTINGS" --parallel 1 --verbosity 2 \
  "${LABELS[@]}" >"$LOG" 2>&1
set -e
# print the full runtests log
echo "run_state_django: ===== BEGIN FULL RUN LOG ====="
cat "$LOG" 2>/dev/null
echo "run_state_django: ===== END FULL RUN LOG ====="
python3 /usr/local/bin/django_to_junit.py "$LOG" "$JUNIT"
