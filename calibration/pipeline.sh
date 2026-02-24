#!/usr/bin/env bash
# =============================================================================
# pipeline.sh — End-to-end calibration pipeline for online-boutique.
#
# Runs stages in order:
#   1. collect.sh           → raw_stats/<service>.txt
#   2. fit.py               → fitted_params.json
#   3. generate_config.py   → online_boutique.yaml
#   4. (optional) run_experiment.py  → sim_output/
#   5. (optional) compare.py         → calibration_report.txt
#
# Usage:
#   ./pipeline.sh                            # collect (60s) + fit + generate
#   ./pipeline.sh --wait 30                  # shorter collection window
#   RUN_SIM=1 ./pipeline.sh                  # also run the simulator
#   RUN_SIM=1 COMPARE=1 ./pipeline.sh        # full pipeline with comparison + plot
#   DURATION=300 ./pipeline.sh               # longer simulation duration
#   ./pipeline.sh --no-collect               # skip collect/fit/generate; just re-run sim + compare
#   RUN_SIM=1 COMPARE=1 ./pipeline.sh --no-collect   # re-run sim + compare only
#
# Environment variables:
#   WAIT_SECS   Envoy counter window in seconds (default: 60)
#   RUN_SIM     Set to 1 to run the simulator after generating YAML (default: 0)
#   COMPARE     Set to 1 to run compare.py after simulation (default: 0;
#               requires RUN_SIM=1)
#   DURATION    Simulator duration in seconds (default: 120)
#   RPS         Override frontend base_rps (default: use value from collect)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../prototype/k8s-config.sh"

# ── Configuration ─────────────────────────────────────────────────────────────
WAIT_SECS="${WAIT_SECS:-60}"
RUN_SIM="${RUN_SIM:-0}"
COMPARE="${COMPARE:-0}"
DURATION="${DURATION:-120}"
RPS_OVERRIDE="${RPS:-}"
COLLECT=1   # set to 0 via --no-collect to skip stages 1-3

RAW_STATS_DIR="${SCRIPT_DIR}/data/raw_stats"
FITTED_PARAMS="${SCRIPT_DIR}/data/fitted_params.json"
GENERATED_YAML="${SCRIPT_DIR}/configs/online_boutique.yaml"
SIM_OUTPUT_DIR="${SCRIPT_DIR}/data/sim_output"

# ── Colours & helpers ─────────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()   { echo -e "${CYAN}[pipeline]${NC} $*"; }
banner() { echo -e "\n${GREEN}=== $* ===${NC}\n"; }

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait)       WAIT_SECS="$2";     shift 2 ;;
        --duration)   DURATION="$2";      shift 2 ;;
        --rps)        RPS_OVERRIDE="$2";  shift 2 ;;
        --run-sim)    RUN_SIM=1;          shift ;;
        --compare)    COMPARE=1;          shift ;;
        --no-collect) COLLECT=0; RUN_SIM=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ── Stages 1–3: Collect / Fit / Generate (skipped with --no-collect) ──────────
if [[ "${COLLECT}" == "1" ]]; then
    banner "Stage 1/3: Collect Envoy stats (${WAIT_SECS}s window)"
    bash "${SCRIPT_DIR}/collect.sh" --wait "${WAIT_SECS}" --output "${RAW_STATS_DIR}"

    banner "Stage 2/3: Fit simulator parameters"
    python3 "${SCRIPT_DIR}/fit.py" "${RAW_STATS_DIR}" --out "${FITTED_PARAMS}"

    banner "Stage 3/3: Generate simulator YAML"
    RPS_ARG=""
    [[ -n "${RPS_OVERRIDE}" ]] && RPS_ARG="--rps ${RPS_OVERRIDE}"

    python3 "${SCRIPT_DIR}/generate_config.py" "${FITTED_PARAMS}" \
        --out "${GENERATED_YAML}" \
        --duration "${DURATION}" \
        ${RPS_ARG}

    info "Generated: ${GENERATED_YAML}"
else
    info "Skipping collect/fit/generate (--no-collect); using existing:"
    info "  ${FITTED_PARAMS}"
    info "  ${GENERATED_YAML}"
    if [[ ! -f "${GENERATED_YAML}" ]]; then
        echo "Error: ${GENERATED_YAML} not found. Run without --no-collect first." >&2
        exit 1
    fi
fi

# ── Optional Stage 4: Run simulator ───────────────────────────────────────────
if [[ "${RUN_SIM}" == "1" ]]; then
    banner "Stage 4: Run simulator"

    SIMULATOR_BIN="${SCRIPT_DIR}/../simulator/bin/run_experiment.py"
    if [[ ! -f "${SIMULATOR_BIN}" ]]; then
        echo "Error: simulator not found at ${SIMULATOR_BIN}" >&2
        exit 1
    fi

    mkdir -p "${SIM_OUTPUT_DIR}"
    python3 "${SIMULATOR_BIN}" "${GENERATED_YAML}" \
        --output "${SIM_OUTPUT_DIR}" \
        --verbose

    info "Simulation output: ${SIM_OUTPUT_DIR}/"

    # ── Optional Stage 5: Compare ──────────────────────────────────────────────
    if [[ "${COMPARE}" == "1" ]]; then
        banner "Stage 5: Compare simulator vs prototype"

        SIM_METRICS="${SIM_OUTPUT_DIR}/service_metrics.csv"
        if [[ ! -f "${SIM_METRICS}" ]]; then
            echo "Warning: ${SIM_METRICS} not found — skipping comparison" >&2
        else
            python3 "${SCRIPT_DIR}/compare.py" \
                "${FITTED_PARAMS}" \
                "${SIM_METRICS}" \
                --report "${SCRIPT_DIR}/reports/calibration_report.txt" \
                --plot-dir "${SCRIPT_DIR}/reports" || true
        fi
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}=== Pipeline complete ===${NC}"
echo ""
echo "  Fitted params:     ${FITTED_PARAMS}"
echo "  Simulator YAML:    ${GENERATED_YAML}"
if [[ "${RUN_SIM}" == "1" ]]; then
    echo "  Sim output:        ${SIM_OUTPUT_DIR}/"
fi
if [[ "${COMPARE}" == "1" && -f "${SCRIPT_DIR}/reports/calibration_report.txt" ]]; then
    echo "  Comparison report: ${SCRIPT_DIR}/reports/calibration_report.txt"
    echo "  Latency plots:     ${SCRIPT_DIR}/reports/latency_comparison.png"
fi
echo ""
echo "  To run the simulator manually:"
echo "    python3 ../simulator/bin/run_experiment.py ${GENERATED_YAML} --output data/sim_output/ --verbose"
echo ""
echo "  To add fault injection and study retries, edit the generated YAML:"
echo "    partial_failures:  # under productcatalogservice"
echo "      - type: partial_failure"
echo "        start_s: 30"
echo "        end_s: 90"
echo "        p_fail: 0.5"
