#!/usr/bin/env bash
# ==========================================================================
# paper.sh — dispatcher for reproducing the paper's experiments.
# ==========================================================================
#
# Usage:
#   ./paper.sh <target> [args]
#
# Targets:
#   setup                      cluster profile + wasm build/upload/serve
#   verify                     verify retry-budget + circuit-breaker applied
#
#   fig2                       Figure 2: all 4 policies, single run
#   fig2-nc                    Figure 2: no-control only (re-run)
#   fig2-cb                    Figure 2: circuit-breaker only (re-run)
#   fig2-rb                    Figure 2: envoy-retry-budget only (re-run)
#   fig2-arolla                Figure 2: arolla only (re-run)
#
#   fig3-load                  Figure 3: RPS sweep
#   fig3-rate                  Figure 3: failure-rate sweep
#   fig3-dur                   Figure 3: fault-duration sweep
#
#   fig5-arolla                Figure 5: arolla parameter sensitivity
#   fig5-cb                    Figure 5: circuit-breaker parameter sensitivity
#   fig5-rb                    Figure 5: retry-budget parameter sensitivity
#
#   plot-fig2    <run_dir>     plot Figure 2 from combined dir
#   plot-fig3-load <dir>       plot Figure 3 RPS panel
#   plot-fig3-rate <dir>       plot Figure 3 failure-rate panel
#   plot-fig3-dur  <dir>       plot Figure 3 fault-duration panel
#   plot-fig6    <rps_dir>     plot Figure 6 (overhead boxplot)
#
# Prereqs:
#   - Cluster deployed (deploy-k8s.sh on primary + second cluster)
#   - Istio >= 1.27 (native retry_budget API support)
#   - 2-replica cluster profile applied
#   - Arolla wasm built + served
#
# Env overrides:
#   NUM_LOADERS (default 4)
# ==========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Common experiment parameters (Figure 2 & per-policy re-runs)
WARMUP=30
PREFAULT=60
FAULT=10
RECOVERY=60
COOLDOWN=10
PROFILE="post-cart-stress-open"
FAULT_MANIFEST="cartservice-100pct"
ALL_POLICIES="no-control,circuit-breaker,envoy-retry-budget,arolla"

export NUM_LOADERS="${NUM_LOADERS:-4}"

run_experiment() {
  local policies="$1"
  NUM_LOADERS="${NUM_LOADERS}" "${SCRIPT_DIR}/run-experiment.sh" \
    --policies "${policies}" \
    --client-profiles "${PROFILE}" \
    -F "${FAULT_MANIFEST}" \
    --warmup "${WARMUP}" --prefault "${PREFAULT}" --fault "${FAULT}" \
    --recovery "${RECOVERY}" --cooldown "${COOLDOWN}"
}

run_sweep() {
  local yaml="$1"
  NUM_LOADERS="${NUM_LOADERS}" "${SCRIPT_DIR}/run_sweep.sh" \
    "${SCRIPT_DIR}/sweeps/${yaml}"
}

plot() {
  python3 "${SCRIPT_DIR}/paper_plotting.py" "$@"
}

target="${1:-}"
shift || true

case "${target}" in
  setup)
    "${PROTO_DIR}/deploy-cluster-profile.sh" 2-replica
    "${PROTO_DIR}/deploy-policy.sh" build-wasm
    "${PROTO_DIR}/deploy-policy.sh" upload-wasm
    "${PROTO_DIR}/deploy-policy.sh" serve-wasm
    ;;

  verify)
    "${PROTO_DIR}/deploy-policy.sh" switch envoy-retry-budget
    "${SCRIPT_DIR}/verify_retry_budget.sh"
    "${PROTO_DIR}/deploy-policy.sh" switch circuit-breaker
    "${SCRIPT_DIR}/verify_circuit_breaker.sh"
    ;;

  # ---- Figure 2 ---------------------------------------------------------
  fig2)        run_experiment "${ALL_POLICIES}" ;;
  fig2-nc)     run_experiment "no-control" ;;
  fig2-cb)     run_experiment "circuit-breaker" ;;
  fig2-rb)     run_experiment "envoy-retry-budget" ;;
  fig2-arolla) run_experiment "arolla" ;;

  # ---- Figure 3 ---------------------------------------------------------
  fig3-load) run_sweep "rps_sweep.yaml" ;;
  fig3-rate) run_sweep "failure_rate_sweep.yaml" ;;
  fig3-dur)  run_sweep "failure_duration_sweep.yaml" ;;

  # ---- Figure 5 (sensitivity) ------------------------------------------
  fig5-arolla) run_sweep "arolla-sensitivity.yaml" ;;
  fig5-cb)     run_sweep "cb-sensitivity.yaml" ;;
  fig5-rb)     run_sweep "rb-sensitivity.yaml" ;;

  # ---- Plotting --------------------------------------------------------
  plot-fig2)
    [[ $# -ge 1 ]] || { echo "usage: $0 plot-fig2 <run_dir>"; exit 2; }
    plot "$1"
    ;;
  plot-fig3-load)
    [[ $# -ge 1 ]] || { echo "usage: $0 plot-fig3-load <combined_dir>"; exit 2; }
    plot --sweep-rps "$1"
    ;;
  plot-fig3-rate)
    [[ $# -ge 1 ]] || { echo "usage: $0 plot-fig3-rate <combined_dir>"; exit 2; }
    plot --sweep-failure-rate "$1"
    ;;
  plot-fig3-dur)
    [[ $# -ge 1 ]] || { echo "usage: $0 plot-fig3-dur <combined_dir>"; exit 2; }
    plot --sweep-fault-duration --x-max 30 "$1"
    ;;
  plot-fig6)
    [[ $# -ge 1 ]] || { echo "usage: $0 plot-fig6 <rps_combined_dir>"; exit 2; }
    plot --overhead-boxplot --y-min 5 --x-max 1200 "$1"
    ;;

  ""|-h|--help|help)
    sed -n '2,40p' "$0"
    ;;
  *)
    echo "unknown target: ${target}" >&2
    echo "run '$0 help' for usage" >&2
    exit 2
    ;;
esac
