#!/usr/bin/env bash
set -euo pipefail

# Coverage gate for the deepseek-review Python script.
# Runs tests with coverage and enforces 95% threshold.

echo "=== Running tests with coverage ==="
python3 -m coverage run -m unittest discover -s tests -v

echo ""
echo "=== Coverage report ==="
python3 -m coverage report --include="scripts/*" --fail-under=95

echo ""
echo "=== Coverage gate PASSED ==="
