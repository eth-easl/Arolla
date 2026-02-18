#!/usr/bin/env bash
# Run key experiments that demonstrate core simulator behaviors.
#
# Usage:
#   bash bin/run_key_experiments.sh          # run all
#   bash bin/run_key_experiments.sh motivation motivation-detail  # run specific ones

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
EXPERIMENTS=(
    "motivation"
    "motivation-detail"
    "motivation-mixed"
    "motivation-varied"
    "multi-client"
)

# If arguments provided, use them instead of default list
if [[ $# -gt 0 ]]; then
    EXPERIMENTS=("$@")
fi

YAML_DIR="experiments/yaml/industry_retreat"
PASSED=0
FAILED=0
TOTAL=${#EXPERIMENTS[@]}

echo "========================================"
echo "Running ${TOTAL} key experiments"
echo "========================================"
echo

for exp in "${EXPERIMENTS[@]}"; do
    yaml_file="${YAML_DIR}/${exp}.yaml"

    if [[ ! -f "$yaml_file" ]]; then
        echo "⚠ Skipping ${exp}: ${yaml_file} not found"
        ((FAILED++))
        continue
    fi

    echo "────────────────────────────────────────"
    echo "▶ ${exp}"
    echo "────────────────────────────────────────"

    if $PYTHON bin/workflow.py "$yaml_file"; then
        echo "✓ ${exp} completed"
        ((PASSED++))
    else
        echo "✗ ${exp} failed"
        ((FAILED++))
    fi
    echo
done

echo "========================================"
echo "Results: ${PASSED}/${TOTAL} passed, ${FAILED} failed"
echo "========================================"
