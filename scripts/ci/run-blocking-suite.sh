#!/usr/bin/env bash
# Runs a CI-blocking test suite that is allowed to be *empty* right now (its owning task
# hasn't landed yet) but must never be *missing*. Exit code 5 from pytest means "no tests
# collected" — tolerated only while the directory legitimately has none yet. Any other
# non-zero exit (including pytest's usage error for a directory that doesn't exist at
# all) fails the job. This is what makes "wired into CI even while empty" true without
# turning a currently-empty suite into a permanently green rubber stamp once it gains
# real tests.
set -euo pipefail

suite_dir="$1"
shift

set +e
uv run pytest "$suite_dir" --no-header -q "$@"
code=$?
set -e

if [ "$code" -eq 0 ] || [ "$code" -eq 5 ]; then
  exit 0
fi
exit "$code"
