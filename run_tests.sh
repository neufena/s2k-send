#!/bin/sh
# Runs every test (simulator only, no hardware). Needs sox for the conversion cases.
# Exits non-zero if any test fails. Set PYTHON to use another interpreter.
cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-python3}"
fail=0
out=$(mktemp) || exit 1
trap 'rm -f "$out"' EXIT

matches=$("$PY" tests/parity.py tests/parity.reference.out | grep -c MATCH)
if [ "$matches" -eq 11 ]; then echo "parity OK (11 frames)"; else echo "parity FAILED ($matches/11 frames)"; fail=1; fi

for t in test_verify test_batch_rates test_pairs test_piscsi test_batch_mode test_server; do
    if "$PY" "tests/$t.py" >"$out" 2>&1; then
        grep -v '^[0-9][0-9]/' "$out" | tail -1
    else
        echo "$t FAILED:"; tail -20 "$out"; fail=1
    fi
done
exit $fail
