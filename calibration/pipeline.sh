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
#   ORCHESTRATE_CLIENTS  Set to 1 to start external clients on CLIENT_HOST
#   CLIENT_DURATION      Auto-stop clients after N seconds (default: WAIT_SECS+15)
#   CLIENT_PROFILES      Comma-separated profiles for run-clients.sh (default: all)
#   ALLOW_SHORT_CLIENT_DURATION  Set to 1 to allow CLIENT_DURATION < WAIT_SECS
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
ORCHESTRATE_CLIENTS="${ORCHESTRATE_CLIENTS:-0}"
CLIENT_DURATION="${CLIENT_DURATION:-}"
CLIENT_PROFILES="${CLIENT_PROFILES:-}"
ALLOW_SHORT_CLIENT_DURATION="${ALLOW_SHORT_CLIENT_DURATION:-0}"
CLIENT_RUNNER="${SCRIPT_DIR}/../prototype/clients/online-boutique/run-clients.sh"
CLIENTS_STARTED_BY_PIPELINE=0
CLIENT_STOPPER_PID=""

RAW_STATS_DIR="${SCRIPT_DIR}/data/raw_stats"
FITTED_PARAMS="${SCRIPT_DIR}/data/fitted_params.json"
GENERATED_YAML="${SCRIPT_DIR}/configs/online_boutique.yaml"
SIM_OUTPUT_DIR="${SCRIPT_DIR}/data/sim_output"

# ── Colours & helpers ─────────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()   { echo -e "${CYAN}[pipeline]${NC} $*"; }
banner() { echo -e "\n${GREEN}=== $* ===${NC}\n"; }

run_clients_cmd() {
    local subcmd="$1"; shift || true
    if [[ ! -x "${CLIENT_RUNNER}" ]]; then
        echo "Error: client runner not found or not executable: ${CLIENT_RUNNER}" >&2
        exit 1
    fi
    if [[ -n "${CLIENT_PROFILES}" ]]; then
        PROFILES="${CLIENT_PROFILES}" "${CLIENT_RUNNER}" "${subcmd}" "$@"
    else
        "${CLIENT_RUNNER}" "${subcmd}" "$@"
    fi
}

cleanup_clients() {
    # Cancel background auto-stopper if still running
    if [[ -n "${CLIENT_STOPPER_PID}" ]]; then
        kill "${CLIENT_STOPPER_PID}" 2>/dev/null || true
        wait "${CLIENT_STOPPER_PID}" 2>/dev/null || true
    fi
    # Stop only if this pipeline started the clients
    if [[ "${CLIENTS_STARTED_BY_PIPELINE}" == "1" ]]; then
        local remote_pid_file="/tmp/online-boutique-clients/traffic_gen.pid"
        local has_pid_file
        has_pid_file=$(ssh ${SSH_OPTS} ${SSH_USER}@${CLIENT_HOST} "test -f '${remote_pid_file}' && echo yes || echo no" 2>/dev/null || echo "no")
        if [[ "${has_pid_file}" == "yes" ]]; then
            info "Stopping external clients started by pipeline..."
            run_clients_cmd stop || true
        fi
    fi
}

trap cleanup_clients EXIT

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait)       WAIT_SECS="$2";     shift 2 ;;
        --duration)   DURATION="$2";      shift 2 ;;
        --rps)        RPS_OVERRIDE="$2";  shift 2 ;;
        --run-sim)    RUN_SIM=1;          shift ;;
        --compare)    COMPARE=1;          shift ;;
        --no-collect) COLLECT=0; RUN_SIM=1; shift ;;
        --with-clients) ORCHESTRATE_CLIENTS=1; shift ;;
        --client-duration) CLIENT_DURATION="$2"; ORCHESTRATE_CLIENTS=1; shift 2 ;;
        --client-profiles) CLIENT_PROFILES="$2"; ORCHESTRATE_CLIENTS=1; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "${CLIENT_DURATION}" ]]; then
    # Cover collect.sh reset overhead + WAIT_SECS + stats fetch overhead.
    CLIENT_DURATION=$((WAIT_SECS + 15))
fi

if [[ "${ORCHESTRATE_CLIENTS}" == "1" ]]; then
    # Integer compare is sufficient here (CLI args are documented as seconds).
    if (( CLIENT_DURATION < WAIT_SECS )); then
        if [[ "${ALLOW_SHORT_CLIENT_DURATION}" == "1" ]]; then
            warn "CLIENT_DURATION (${CLIENT_DURATION}s) < WAIT_SECS (${WAIT_SECS}s): traffic will stop before collection window ends."
        else
            echo "Error: CLIENT_DURATION (${CLIENT_DURATION}s) is shorter than WAIT_SECS (${WAIT_SECS}s)." >&2
            echo "This will undercount RPS and degrade calibration quality." >&2
            echo "Fix by using --wait <= --client-duration (e.g. --wait ${CLIENT_DURATION})" >&2
            echo "Or override intentionally with ALLOW_SHORT_CLIENT_DURATION=1." >&2
            exit 1
        fi
    fi
fi

maybe_start_clients() {
    [[ "${ORCHESTRATE_CLIENTS}" == "1" ]] || return 0
    banner "External clients: start on CLIENT_HOST"
    info "Client runner: ${CLIENT_RUNNER}"
    info "Client duration: ${CLIENT_DURATION}s (auto-stop)"
    [[ -n "${CLIENT_PROFILES}" ]] && info "Client profiles: ${CLIENT_PROFILES}"

    local start_out=""
    if ! start_out="$(run_clients_cmd start 2>&1)"; then
        echo "${start_out}"
        echo "Error: failed to start external clients" >&2
        exit 1
    fi
    echo "${start_out}"
    if echo "${start_out}" | grep -q "started pid="; then
        CLIENTS_STARTED_BY_PIPELINE=1
    else
        CLIENTS_STARTED_BY_PIPELINE=0
        info "Clients were already running; pipeline will not stop them automatically."
    fi

    # Schedule an auto-stop as a safety net. Only the clients we started are stopped.
    if [[ "${CLIENTS_STARTED_BY_PIPELINE}" == "1" ]]; then
        (
            sleep "${CLIENT_DURATION}"
            "${CLIENT_RUNNER}" stop >/dev/null 2>&1 || true
        ) &
        CLIENT_STOPPER_PID="$!"
    fi
}

post_fit_sanity_check() {
    [[ -f "${FITTED_PARAMS}" ]] || return 0

    # Extract frontend rps from fitted_params.json (stdlib only).
    local frontend_rps
    frontend_rps=$(python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    v = data.get("services", {}).get("frontend", {}).get("rps")
    print("" if v is None else v)
except Exception:
    print("")
' "${FITTED_PARAMS}" 2>/dev/null || true)
    frontend_rps="$(echo "${frontend_rps}" | tr -d '[:space:]')"
    [[ -z "${frontend_rps}" ]] && return 0

    # Warn when frontend traffic is too low to produce a stable calibration.
    if python3 -c 'import sys; sys.exit(0 if float(sys.argv[1]) < 1.0 else 1)' "${frontend_rps}" >/dev/null 2>&1
    then
        warn "Observed frontend RPS is low (${frontend_rps}). Calibration may be noisy or underfit."
        warn "Check client traffic and ensure --wait <= --client-duration when using --with-clients."
    fi
}

# ── Stages 1–3: Collect / Fit / Generate (skipped with --no-collect) ──────────
if [[ "${COLLECT}" == "1" ]]; then
    maybe_start_clients

    banner "Stage 1/3: Collect Envoy stats (${WAIT_SECS}s window)"
    bash "${SCRIPT_DIR}/collect.sh" --wait "${WAIT_SECS}" --output "${RAW_STATS_DIR}"

    banner "Stage 2/3: Fit simulator parameters"
    python3 "${SCRIPT_DIR}/fit.py" "${RAW_STATS_DIR}" --out "${FITTED_PARAMS}"
    post_fit_sanity_check

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
