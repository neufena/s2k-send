#!/bin/sh
# Runs every test (simulator only, no hardware). Needs sox for the conversion cases.
cd "$(dirname "$0")" || exit 1
python3 tests/parity.py tests/parity.reference.out | grep -c MATCH
for t in test_verify test_batch_rates test_pairs test_piscsi test_batch_mode test_server; do python3 tests/$t.py 2>&1 | grep -v '^[0-9][0-9]/' | tail -1; done
