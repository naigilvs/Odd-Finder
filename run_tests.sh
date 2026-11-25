#!/bin/sh
# Runs every Odd Finder test. No internet or API key needed.
cd "$(dirname "$0")" || exit 1
echo "== Server and stats (Python) =="
python3 -m unittest discover -s tests -p "test_*.py" || exit 1
echo
echo "== Page calculations (Node.js) =="
if command -v node >/dev/null 2>&1; then
  node --test tests/test_calc.mjs || exit 1
else
  echo "Node.js isn't installed, so the page tests were skipped."
fi
echo
echo "All tests passed."
