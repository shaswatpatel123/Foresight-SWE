#!/usr/bin/env bash
# Run changed Go test packages; write JUnit.
#   go_run_tests.sh <junit_out> <test_file> [test_file...]
# -count=1 disables the test cache.
set -uo pipefail

JUNIT="$1"; shift
TESTS=("$@")
REPO_DIR="${REPO_DIR:-/testbed}"
GO_TEST_TIMEOUT="${GO_TEST_TIMEOUT:-600s}"
cd "$REPO_DIR"

# group packages by owning go.mod
declare -A MOD_PKGS
for f in "${TESTS[@]}"; do
  [ -e "$f" ] || { echo "go_run_tests: missing $f (deleted at this state)"; continue; }
  dir="$(dirname "$f")"
  mod="$dir"
  while [ "$mod" != "." ] && [ ! -f "$mod/go.mod" ]; do mod="$(dirname "$mod")"; done
  [ -f "$mod/go.mod" ] || mod="."
  if [ "$mod" = "." ]; then rel="./$dir"; else rel="./${dir#"$mod"/}"; fi
  case " ${MOD_PKGS[$mod]:-} " in
    *" $rel "*) ;;
    *) MOD_PKGS[$mod]="${MOD_PKGS[$mod]:-} $rel" ;;
  esac
done

if [ ${#MOD_PKGS[@]} -eq 0 ]; then
  echo "go_run_tests: no runnable packages"
  python3 /usr/local/bin/go_to_junit.py "$JUNIT" /dev/null
  exit 0
fi

EVENTS=$(mktemp -d)/events.jsonl
: > "$EVENTS"
for mod in "${!MOD_PKGS[@]}"; do
  pkgs=${MOD_PKGS[$mod]}
  echo "go_run_tests: module=$mod packages=$pkgs"
  ( cd "$mod" && go test -json -count=1 -timeout="$GO_TEST_TIMEOUT" $pkgs ) >> "$EVENTS" 2>>"$EVENTS.err"
done
# print full stderr
if [ -s "$EVENTS.err" ]; then
  echo "go_run_tests: ===== BEGIN FULL RUN LOG ====="
  cat "$EVENTS.err"
  echo "go_run_tests: ===== END FULL RUN LOG ====="
fi

python3 /usr/local/bin/go_to_junit.py "$JUNIT" "$EVENTS"
