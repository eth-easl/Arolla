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
# all policies (10s fault, 60s recovery) take about 22 minutes
# ==========================================================================
effectiveness_experiment()            { run_experiment "${ALL_POLICIES}"; }
effectiveness_no_control_only()       { run_experiment "no-control"; }
effectiveness_circuit_breaker_only()  { run_experiment "circuit-breaker"; }
effectiveness_retry_budget_only()     { run_experiment "envoy-retry-budget"; }
effectiveness_arolla_only()           { run_experiment "arolla"; }

# ==========================================================================
# Per-tenant fairness: multi-profile fan-out exercising the
# fairness bucket with heterogeneous retry loads.
# ==========================================================================
fairness_experiment_same_rps() {
  exec_cmd NUM_LOADERS="${NUM_LOADERS}" \
    "${SCRIPT_DIR}/run-experiment.sh" \
    --policies "arolla,arolla-fairness" \
    --client-profiles "fairness-same-rps/client1,fairness-same-rps/client2,fairness-same-rps/client3,fairness-same-rps/client4,fairness-same-rps/client5,fairness-same-rps/client6" \
    -F "${FAULT_MANIFEST}" \
    --warmup 30 --prefault 60 --fault 20 --recovery 60 --cooldown 10
}

fairness_experiment_diff_rps() {
  exec_cmd NUM_LOADERS="${NUM_LOADERS}" \
    "${SCRIPT_DIR}/run-experiment.sh" \
    --policies "arolla,arolla-fairness" \
    --client-profiles "fairness-diff-rps/client1,fairness-diff-rps/client2,fairness-diff-rps/client3,fairness-diff-rps/client4,fairness-diff-rps/client5,fairness-diff-rps/client6" \
    -F "${FAULT_MANIFEST}" \
    --warmup 30 --prefault 60 --fault 20 --recovery 60 --cooldown 10
}

# ==========================================================================
# Recovery time vs. workload / failure characteristics
# load sweep with two policies takes about 1h30min
# failure rate sweep with two policies takes about 2h40min
# fault duration sweep with two policies takes about 7h
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
# retry budget grid takes about 4h
# ==========================================================================
retry_budget_grid()           { run_grid "rb-grid.yaml"; }
arolla_grid()                 { run_grid "arolla-grid.yaml"; }

# ==========================================================================
# Plotting — re-plot from an existing output dir without re-running.
# ==========================================================================
plot_effectiveness()             { plot_py "${1:?usage: plot_effectiveness <run_dir>}"; }
plot_recovery_vs_load()          { plot_py --sweep-rps "${1:?usage: plot_recovery_vs_load <combined_dir>}"; }
plot_recovery_vs_failure_rate()  { plot_py --sweep-failure-rate "${1:?usage: plot_recovery_vs_failure_rate <combined_dir>}"; }
plot_recovery_vs_fault_duration(){ plot_py --sweep-fault-duration --x-max 60 "${1:?usage: plot_recovery_vs_fault_duration <combined_dir>}"; }
plot_overhead()                  { plot_py --overhead-boxplot --y-min 5 --x-max 1200 "${1:?usage: plot_overhead <rps_combined_dir>}"; }

# Multirun plots: aggregate multiple sweep runs and show min-max shaded
# bands + "no recovery" markers for points where any run failed. <parent>
# is the directory that contains per-run subdirs (e.g., outputs/nsdi/
# rps_sweep). Optional <exclude_pattern> is a comma-separated list of
# substrings; run-dir names containing any of these are skipped. Defaults
# below match the conventions used in the NSDI runs and may need tweaking
# as new variants are added.
plot_recovery_vs_load_multirun() {
  local parent="${1:?usage: plot_recovery_vs_load_multirun <parent_dir> [exclude_pattern]}"
  local exclude="${2:-_rb_10%}"
  plot_py --sweep-rps-multirun "${parent}" --x-max 1600 \
    --include "${ALL_POLICIES}" --exclude-dir "${exclude}"
}

plot_recovery_vs_failure_rate_multirun() {
  local parent="${1:?usage: plot_recovery_vs_failure_rate_multirun <parent_dir> [exclude_pattern]}"
  local exclude="${2:-fairness,no_gateway}"
  plot_py --sweep-failure-rate-multirun "${parent}" \
    --include "${ALL_POLICIES}" --exclude-dir "${exclude}"
}

plot_recovery_vs_fault_duration_multirun() {
  local parent="${1:?usage: plot_recovery_vs_fault_duration_multirun <parent_dir> [exclude_pattern]}"
  local exclude="${2:-rps1000,rps1400,test}"
  plot_py --sweep-fault-duration-multirun "${parent}" --x-max 60 \
    --include "${ALL_POLICIES}" --exclude-dir "${exclude}"
}

# Grid-sensitivity heatmap for a 2D/3D cartesian grid (rb-grid, arolla-grid).
# Usage:
#   plot_grid_sensitivity <grid-yaml-basename> <sweep-output-dir>
# Example:
#   ./paper.sh plot_grid_sensitivity rb-grid.yaml \
#     outputs/nsdi/rb-grid/20260415_014914/post-cart-stress-open
plot_grid_sensitivity() {
  local yaml="${1:?usage: plot_grid_sensitivity <grid-yaml-basename> <sweep-output-dir>}"
  local sweep_dir="${2:?usage: plot_grid_sensitivity <grid-yaml-basename> <sweep-output-dir>}"
  exec_cmd python3 "${SCRIPT_DIR}/plot_grid_sensitivity.py" \
    "${SCRIPT_DIR}/sweeps/${yaml}" "${sweep_dir}"
}

# Per-tenant fairness two-panel plot (same-rps vs diff-rps).
# Usage:
#   plot_fairness <same_rps_dir> <diff_rps_dir> [metric]
# If metric omitted, renders all three (count, rate, sod). Output PDFs go
# next to the input dirs (outputs/nsdi/fairness-{same,diff}-rps-<metric>.pdf).
plot_fairness() {
  local same="${1:?usage: plot_fairness <same_rps_dir> <diff_rps_dir> [metric]}"
  local diff="${2:?usage: plot_fairness <same_rps_dir> <diff_rps_dir> [metric]}"
  local metrics
  if [[ $# -ge 3 && -n "$3" ]]; then metrics=("$3"); else metrics=(count rate sod); fi
  local outroot="${OUTPUT_BASE:-${REPO_ROOT}/outputs/nsdi}"
  for m in "${metrics[@]}"; do
    exec_cmd python3 "${SCRIPT_DIR}/plot_fairness_two_panel.py" \
      "$same" "$diff" \
      --metric "$m" \
      --same-output "${outroot}/fairness-same-rps-${m}.pdf" \
      --diff-output "${outroot}/fairness-diff-rps-${m}.pdf"
  done
}

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
    # plot_* targets may need multiple args (dirs, metric, etc.). Feed them
    # all remaining positional args up to the next registered target name,
    # so `plot_fairness A B count plot_overhead D` routes args correctly.
    if [[ "${target}" == plot_* ]]; then
      local plot_args=()
      while [[ $# -gt 0 ]]; do
        declare -F "$1" >/dev/null && break   # stop at next target
        plot_args+=("$1"); shift
      done
      "${target}" "${plot_args[@]}"
    else
      "${target}"
    fi
  done
}

main "$@"


# python3 prototype/experiments/plot_arolla_sensitivity_multirun.py outputs/nsdi/arolla-sensitivity --fault 10 25 2>&1

# python3 prototype/experiments/plot_rb_sensitivity_multirun.py outputs/nsdi/rb-sensitivity --fault 10 2>&1
