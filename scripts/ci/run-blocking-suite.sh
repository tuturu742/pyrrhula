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

report="$(mktemp -t blocking-suite-XXXXXX.xml)"
trap 'rm -f "$report"' EXIT

set +e
uv run pytest "$suite_dir" --no-header -q --junitxml="$report" "$@"
code=$?
set -e

if [ "$code" -ne 0 ] && [ "$code" -ne 5 ]; then
  exit "$code"
fi

# A suite whose every test skipped itself exits 0 and reports green while asserting
# nothing -- which is how the replay job spent months proving nothing about INV-10.
# "Empty because the work hasn't landed" (exit 5, no report) stays tolerated; "collected
# real tests and ran none of them" does not.
if [ "$code" -eq 0 ] && [ -s "$report" ]; then
  uv run python - "$report" "$suite_dir" <<'PYTHON'
import sys
import xml.etree.ElementTree as ET

report, suite_dir = sys.argv[1], sys.argv[2]
suite = ET.parse(report).getroot().find("testsuite")
total = int(suite.get("tests", 0))
skipped = int(suite.get("skipped", 0))
if total and total == skipped:
    sys.exit(
        f"{suite_dir}: all {total} tests skipped themselves, so this blocking suite "
        f"asserted nothing while reporting green. Give the job what the suite needs "
        f"(usually a database) rather than accepting the pass."
    )
PYTHON
fi

exit 0
