#!/usr/bin/env bash
# ==========================================================================
# paper.sh — named experiments for the paper. Pick one (or more) to run.
#
# Usage:
#   ./paper.sh                                     # show help + list of targets
#   ./paper.sh effectiveness_retry_budget_only     # re-run a single policy
#   ./paper.sh recovery_vs_load recovery_vs_fault_duration
#   ./paper.sh plot_effectiveness <run_dir>        # re-plot from existing data
#   ./paper.sh --dry-run <target>                  # print commands without running
#   ./paper.sh --list                              # list only target names
# ==========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"


export OUTPUT_BASE="${OUTPUT_BASE:-${REPO_ROOT}/outputs/nsdi}"

# ---- shared knobs ---------------------------------------------------------
export NUM_LOADERS="${NUM_LOADERS:-4}"
PROFILE="post-cart-stress-open"
FAULT_MANIFEST="cartservice-100pct"
WARMUP=30
PREFAULT=60
FAULT=10
RECOVERY=60
COOLDOWN=10
ALL_POLICIES="no-control,circuit-breaker,envoy-retry-budget,arolla"

# Set DRY_RUN=1 (env var) or pass --dry-run to print each command instead of
# executing it. Good for verifying what each target will do before burning a
# 3-hour grid on the cluster.
DRY_RUN="${DRY_RUN:-0}"

# ---- helpers --------------------------------------------------------------
# All external invocations go through `exec_cmd` so DRY_RUN can intercept.
# Leading VAR=val tokens are passed through env(1) — bash's built-in
# variable-prefix syntax only works at parse time, not when reconstituted
# through "$@", so `exec_cmd NUM_LOADERS=4 cmd` needs env to take effect.
exec_cmd() {
  local env_args=()
  while [[ $# -gt 0 && "$1" =~ ^[A-Za-z_][A-Za-z_0-9]*= ]]; do
    env_args+=("$1"); shift
  done
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '[dry-run]'
    (( ${#env_args[@]} > 0 )) && printf ' %q' "${env_args[@]}"
    printf ' %q' "$@"
    printf '\n'
  elif (( ${#env_args[@]} > 0 )); then
    env "${env_args[@]}" "$@"
  else
    "$@"
  fi
}

run_experiment() {
  local policies="$1"
  # env-prefix so NUM_LOADERS is visible at the call site (and in dry-run
  # output), not just silently inherited from the exported env.
  exec_cmd NUM_LOADERS="${NUM_LOADERS}" \
    "${SCRIPT_DIR}/run-experiment.sh" \
    --policies "${policies}" \
    --client-profiles "${PROFILE}" \
    -F "${FAULT_MANIFEST}" \
    --warmup "${WARMUP}" --prefault "${PREFAULT}" --fault "${FAULT}" \
    --recovery "${RECOVERY}" --cooldown "${COOLDOWN}"
}

run_sweep() {
  local yaml="$1"
  exec_cmd NUM_LOADERS="${NUM_LOADERS}" \
    "${SCRIPT_DIR}/run_sweep.sh" "${SCRIPT_DIR}/sweeps/${yaml}"
}

run_grid() {
  local yaml="$1"
  exec_cmd NUM_LOADERS="${NUM_LOADERS}" \
    "${SCRIPT_DIR}/run_grid.sh" "${SCRIPT_DIR}/sweeps/${yaml}"
}

plot_py() { exec_cmd python3 "${SCRIPT_DIR}/paper_plotting.py" "$@"; }

# ==========================================================================
# Effectiveness under sustained partial failure
# ==========================================================================
effectiveness_experiment()            { run_experiment "${ALL_POLICIES}"; }
effectiveness_no_control_only()       { run_experiment "no-control"; }
effectiveness_circuit_breaker_only()  { run_experiment "circuit-breaker"; }
effectiveness_retry_budget_only()     { run_experiment "envoy-retry-budget"; }
effectiveness_arolla_only()           { run_experiment "arolla"; }

# ==========================================================================
# Recovery time vs. workload / failure characteristics
# ==========================================================================
recovery_vs_load()            { run_sweep "rps_sweep.yaml"; }
recovery_vs_failure_rate()    { run_sweep "failure_rate_sweep.yaml"; }
recovery_vs_fault_duration()  { run_sweep "failure_duration_sweep.yaml"; }

# ==========================================================================
# Per-policy parameter sensitivity
# ==========================================================================
arolla_sensitivity()          { run_sweep "arolla-sensitivity.yaml"; }
circuit_breaker_sensitivity() { run_sweep "cb-sensitivity.yaml"; }
retry_budget_sensitivity()    { run_sweep "rb-sensitivity.yaml"; }

# ==========================================================================
# Parameter grids (cartesian product — multi-dimensional sensitivity)
# ==========================================================================
retry_budget_grid()           { run_grid "rb-grid.yaml"; }
arolla_grid()                 { run_grid "arolla-grid.yaml"; }

# ==========================================================================
# Plotting — re-plot from an existing output dir without re-running.
# ==========================================================================
plot_effectiveness()             { plot_py "${1:?usage: plot_effectiveness <run_dir>}"; }
plot_recovery_vs_load()          { plot_py --sweep-rps "${1:?usage: plot_recovery_vs_load <combined_dir>}"; }
plot_recovery_vs_failure_rate()  { plot_py --sweep-failure-rate "${1:?usage: plot_recovery_vs_failure_rate <combined_dir>}"; }
plot_recovery_vs_fault_duration(){ plot_py --sweep-fault-duration --x-max 30 "${1:?usage: plot_recovery_vs_fault_duration <combined_dir>}"; }
plot_overhead()                  { plot_py --overhead-boxplot --y-min 5 --x-max 1200 "${1:?usage: plot_overhead <rps_combined_dir>}"; }

# ==========================================================================
# Dispatcher
# ==========================================================================
list_targets() {
  grep -E '^[a-zA-Z_][a-zA-Z0-9_]*\(\)\s*\{' "$0" \
    | sed -E 's/\(\).*//' \
    | grep -vE '^(exec_cmd|run_experiment|run_sweep|run_grid|plot_py|list_targets|main)$'
}

# in main, pass targets on the CLI — or override the default below
main() {
  # Strip --dry-run / -n from the arg list before dispatching.
  local args=()
  for a in "$@"; do
    case "$a" in
      -n|--dry-run) DRY_RUN=1 ;;
      *) args+=("$a") ;;
    esac
  done
  set -- ${args[@]+"${args[@]}"}
  [[ "${DRY_RUN}" == "1" ]] && echo "[dry-run mode: commands will be printed, not executed]" >&2

  case "${1:-}" in
    -h|--help|"")
      sed -n '2,11p' "$0"
      return
      ;;
    -l|--list) list_targets; return ;;
  esac
  while [[ $# -gt 0 ]]; do
    local target="$1"; shift
    if ! declare -F "${target}" >/dev/null; then
      echo "unknown target: ${target}" >&2
      echo "run '$0 --list' for available targets" >&2
      exit 2
    fi
    # plot_* targets consume the next positional arg (the dir to plot).
    if [[ "${target}" == plot_* ]]; then
      "${target}" "${1:-}"; [[ $# -gt 0 ]] && shift || true
    else
      "${target}"
    fi
  done
}

main "$@"
