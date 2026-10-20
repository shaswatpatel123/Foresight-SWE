#!/usr/bin/env bash
# Run JS/TS tests in the current tree; write JUnit.
#   node_run_tests.sh <junit_out> <test_file> [test_file...]
# Framework is detected from package.json.
set -uo pipefail

JUNIT="$1"; shift
TESTS=("$@")
REPO_DIR="${REPO_DIR:-/testbed}"
INSTALL_DIR="${INSTALL_DIR:-/opt/install}"
cd "$REPO_DIR"

# --- recipe test_cmd: run once per $FILE
RECIPE_SHA="${FORESIGHT_RECIPE_SHA:-}"
TEST_CMD="${FORESIGHT_TEST_CMD:-}"
if [ -z "$TEST_CMD" ] && [ -n "$RECIPE_SHA" ]; then
  R="$INSTALL_DIR/$(echo "$RECIPE_SHA" | cut -c1-12).json"
  [ -f "$R" ] && TEST_CMD=$(python3 - "$R" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
tc = cfg.get("test_cmd")
# only a string test_cmd runs
print(tc if isinstance(tc, str) else "")
PYEOF
)
fi

FRAMEWORK=$(python3 - <<'PYEOF'
import json, os
try:
    pkg = json.load(open(os.path.join(os.environ.get("REPO_DIR", "/testbed"),
                                      "package.json")))
except Exception:
    print("unknown"); raise SystemExit
deps = {}
for k in ("devDependencies", "dependencies"):
    d = pkg.get(k)
    if isinstance(d, dict):
        deps.update(d)
# newest framework first
for name in ("vitest", "jest", "mocha"):
    if name in deps:
        print(name); break
else:
    print("unknown")
PYEOF
)
echo "node_run_tests: framework=$FRAMEWORK  files=${#TESTS[@]}" \
     "$([ -n "$TEST_CMD" ] && echo 'test_cmd=recipe' || echo 'test_cmd=builtin')"
if [ "$FRAMEWORK" = "unknown" ] && [ -z "$TEST_CMD" ]; then
  # test_cmd needs no framework
  echo "node_run_tests: no vitest/jest/mocha in package.json at this commit"
  exit 4
fi

# run each suite once, reuse its XML
declare -A _SUITE_RAW=()
PAIRS=()
i=0
for T in "${TESTS[@]}"; do
  RAW="/tmp/_raw_$i.xml"; i=$((i + 1))
  rm -f "$RAW"
  if [ -n "$TEST_CMD" ]; then
    # key: the expanded command
    _key="$(FILE="$T" JUNIT="$RAW" bash -c "printf '%s' \"$TEST_CMD\"" 2>/dev/null | sed "s|$RAW||g")"
    if [ -n "${_SUITE_RAW[$_key]:-}" ] && [ -f "${_SUITE_RAW[$_key]}" ]; then
      echo "node_run_tests: $T -> reusing suite result (dedup)"
      PAIRS+=("$T" "${_SUITE_RAW[$_key]}")
      continue
    fi
    FILE="$T" JUNIT="$RAW" timeout --kill-after=60 "${STATE_TIMEOUT:-2400}" \
      bash -c "$TEST_CMD" >>/tmp/_run.log 2>&1
    if [ -f "$RAW" ]; then PAIRS+=("$T" "$RAW"); _SUITE_RAW[$_key]="$RAW"; fi
    continue
  fi
  case "$FRAMEWORK" in
    vitest)
      # `run`, not watch mode
      timeout --kill-after=60 "${STATE_TIMEOUT:-2400}" \
        npx --no-install vitest run "$T" --reporter=junit --outputFile="$RAW" \
        >>/tmp/_run.log 2>&1
      ;;
    jest)
      # jest-junit is installed globally
      JEST_JUNIT_OUTPUT_FILE="$RAW" \
      JEST_JUNIT_CLASSNAME="{classname}" \
      JEST_JUNIT_TITLE="{classname} {title}" \
      timeout --kill-after=60 "${STATE_TIMEOUT:-2400}" \
        npx --no-install jest "$T" --reporters=jest-junit --ci >>/tmp/_run.log 2>&1
      ;;
    mocha)
      timeout --kill-after=60 "${STATE_TIMEOUT:-2400}" \
        npx --no-install mocha "$T" --reporter mocha-junit-reporter \
        --reporter-options "mochaFile=$RAW,useFullSuiteTitle=true" \
        >>/tmp/_run.log 2>&1
      ;;
  esac
  # no XML: no outcomes
  [ -f "$RAW" ] && PAIRS+=("$T" "$RAW")
done

# per-file testcase counts
i=0
for T in "${TESTS[@]}"; do
  R="/tmp/_raw_$i.xml"; i=$((i + 1))
  if [ -f "$R" ]; then
    echo "node_run_tests:   $T -> $(grep -o '<testcase' "$R" | wc -l) testcase(s)"
  else
    echo "node_run_tests:   $T -> NO OUTPUT"
  fi
done
# always print the full log
echo "node_run_tests: ===== BEGIN FULL RUN LOG ====="
cat /tmp/_run.log 2>/dev/null
echo "node_run_tests: ===== END FULL RUN LOG ====="

if [ ${#PAIRS[@]} -eq 0 ]; then
  echo "node_run_tests: no framework output for any test file"
  exit 5
fi
python3 /usr/local/bin/node_to_junit.py "$JUNIT" "${PAIRS[@]}"
