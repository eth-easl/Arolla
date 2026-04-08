#!/usr/bin/env bash
# ============================================================================
# run-experiment.sh — end-to-end orchestrator for prototype retry experiments
# ============================================================================
#
# For each policy in --policies, this script runs one full timeline:
#
#   t=0                       start load generator
#   t=+WARMUP                 (warmup done — steady-state reached)
#   t=+WARMUP+PREFAULT        (baseline captured)
#   t=+WARMUP+PREFAULT        kubectl apply <fault manifest>
#   t=...+FAULT               kubectl delete <fault manifest>
#   t=...+RECOVERY            (recovery window captured)
#   t=...+RECOVERY            stop load generator
#   t=...+COOLDOWN            (metrics drained, collected)
#
# Between policy runs it switches the server-side retry policy via
# deploy-policy.sh, so the four paper baselines (no-control, circuit-breaker,
# envoy-retry-budget, arolla) can be measured back-to-back.
#
# Usage:
#
#   prototype/experiments/run-experiment.sh \
#       --scenario sustained-failure \
#       --policies no-control,circuit-breaker,envoy-retry-budget,arolla \
#       --warmup 60 --prefault 30 --fault 60 --recovery 60 --cooldown 30
#
# Run `--help` for the full option list and `--dry-run` to print the timeline
# without touching the cluster.
#
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"
# shellcheck source=../k8s-config.sh
source "${PROTO_DIR}/k8s-config.sh"

NAMESPACE="online-boutique"
FAULT_DIR_REL="manifests/online-boutique"
REMOTE_BASE="${REMOTE_BASE:-/tmp/online-boutique-clients}"
REMOTE_METRICS_DIR="${REMOTE_BASE}/metrics"
REMOTE_PID_FILE="${REMOTE_BASE}/traffic_gen.pid"
REMOTE_LOG_FILE="${REMOTE_BASE}/traffic_gen.log"

# --------------------------------------------------------------------------- #
# Defaults (overridable via CLI and scenario config)
# --------------------------------------------------------------------------- #

SCENARIO="sustained-failure"
POLICIES_CSV="no-control,circuit-breaker,envoy-retry-budget,arolla"
CLIENT_PROFILES=""          # empty → run all profiles from profiles/; else a CSV subset
FAULT_MANIFEST_OVERRIDE=""  # empty → use scenario's default; else override

# CPU-stress trigger (chaos-mesh StressChaos). When CPU_STRESS_TARGET is set,
# the trigger replaces the Istio fault manifest during the fault window.
# Used to reproduce Huang et al. "Metastable Failures in the Wild" (OSDI '22)
# Figure 5: a transient CPU restriction on a stateful component that pushes
# the system from vulnerable into metastable.
CPU_STRESS_TARGET=""        # empty → no CPU stress; else workload label (e.g. "cartservice")
CPU_STRESS_LOAD=80          # % CPU per stress worker (paper Fig 5: 80%=metastable, 78%=recovers)
CPU_STRESS_WORKERS=1        # number of stress-ng worker threads
# Per-run output goes under outputs/prototype/runs/<timestamp>/ at the repo
# root. This sits alongside outputs/{basic,client_count,metastable}/ which
# hold simulator plots, namespaced under outputs/prototype/ to avoid collision.
OUTPUT_ROOT="${REPO_ROOT}/outputs/prototype/runs"
DRY_RUN=false
SKIP_ANALYZE=false
POST_POLICY_SETTLE_SEC=5    # give xDS a moment after switching policies

# Phase durations (seconds). The scenario config can override these; CLI
# flags override both.
WARMUP_SEC=60
PREFAULT_SEC=30
FAULT_SEC=60
RECOVERY_SEC=60
COOLDOWN_SEC=30

# Scenario-provided
SCENARIO_TITLE=""
SCENARIO_FAULT_MANIFEST=""

# --------------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------------- #

C_BLUE='\033[1;34m'
C_YELL='\033[1;33m'
C_RED='\033[1;31m'
C_GREEN='\033[1;32m'
C_OFF='\033[0m'

log()   { printf "${C_BLUE}[exp]${C_OFF} %s\n" "$*" >&2; }
phase() { printf "${C_GREEN}[exp]${C_OFF} %s\n" "$*" >&2; }
warn()  { printf "${C_YELL}[exp]${C_OFF} %s\n" "$*" >&2; }
err()   { printf "${C_RED}[exp]${C_OFF} %s\n" "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# SSH helpers to CLIENT_HOST (via k8s-config.sh)
# --------------------------------------------------------------------------- #

ssh_client() {
  local key_opt=""
  [[ -n "${SSH_KEY}" ]] && key_opt="-i ${SSH_KEY}"
  # shellcheck disable=SC2086
  ssh ${SSH_OPTS} ${key_opt} "${SSH_USER}@${CLIENT_HOST}" "$@"
}

scp_from_client() {
  local src="$1" dst="$2"
  local key_opt=""
  [[ -n "${SSH_KEY}" ]] && key_opt="-i ${SSH_KEY}"
  # shellcheck disable=SC2086
  scp ${SSH_OPTS} ${key_opt} -r "${SSH_USER}@${CLIENT_HOST}:${src}" "${dst}"
}

# --------------------------------------------------------------------------- #
# CLI parsing
# --------------------------------------------------------------------------- #

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Options:
  -s, --scenario <name>         Scenario name (matches scenarios/<name>.conf).
                                Default: ${SCENARIO}
  -p, --policies <a,b,c>        Comma-separated server policy list.
                                Default: ${POLICIES_CSV}
  -c, --client-profiles <a,b>   Comma-separated client profiles to run
                                (matches profiles/<name>.json). Empty = all.
                                Passed as PROFILES env var to run-clients.sh.
                                Default: (all profiles)
  -F, --fault-manifest <name>   Override scenario's fault manifest.
                                Accepts:
                                  cartservice-50pct          (bare name)
                                  cartservice-50pct.yaml     (with extension)
                                  faults/cartservice-50pct.yaml (relative
                                                  to manifests/online-boutique/)
                                Default: whatever the scenario config sets.
      --cpu-stress-target <name>  Apply a chaos-mesh StressChaos to the
                                given workload (pod label app=<name>) during
                                the fault window, INSTEAD OF the Istio fault
                                manifest. Used to reproduce Huang et al.
                                OSDI '22 Figure 5 metastability triggers.
                                Requires chaos-mesh installed cluster-wide.
                                Common target: cartservice
                                Default: (no CPU stress; use fault manifest)
      --cpu-stress-load <pct>     CPU% to consume per stress worker.
                                Paper Figure 5: 80% is metastable for their
                                MongoDB setup; 78% recovers cleanly. The
                                threshold for online-boutique will differ —
                                sweep this value to find it.
                                Default: ${CPU_STRESS_LOAD}
      --cpu-stress-workers <n>    Number of concurrent stress-ng workers.
                                Default: ${CPU_STRESS_WORKERS}
  -o, --output <dir>            Output root directory.
                                Default: ${OUTPUT_ROOT}/<timestamp>
      --warmup <sec>            Override warmup duration
      --prefault <sec>          Override pre-fault baseline duration
      --fault <sec>             Override fault duration
      --recovery <sec>          Override recovery duration
      --cooldown <sec>          Override cooldown duration
      --settle <sec>            Post-policy-switch settle (default: ${POST_POLICY_SETTLE_SEC})
  -n, --dry-run                 Print the timeline, don't touch the cluster
      --skip-analyze            Don't invoke analyze.py at the end
  -h, --help                    Show this message

Available fault manifests under manifests/online-boutique/faults/:
$(find "${PROTO_DIR}/${FAULT_DIR_REL}/faults" -maxdepth 1 -name '*.yaml' -print0 2>/dev/null | xargs -0 -n1 basename 2>/dev/null | sed 's/^/  /' || echo "  (none found)")

Scenarios are bash config files under scenarios/*.conf. They define
SCENARIO_TITLE, SCENARIO_FAULT_MANIFEST (relative to prototype/${FAULT_DIR_REL}/)
and may override any of the phase durations.
EOF
  exit "${1:-0}"
}

while (( $# > 0 )); do
  case "$1" in
    -s|--scenario)        SCENARIO="$2"; shift 2 ;;
    -p|--policies)        POLICIES_CSV="$2"; shift 2 ;;
    -c|--client-profiles) CLIENT_PROFILES="$2"; shift 2 ;;
    -F|--fault-manifest)  FAULT_MANIFEST_OVERRIDE="$2"; shift 2 ;;
    --cpu-stress-target)  CPU_STRESS_TARGET="$2"; shift 2 ;;
    --cpu-stress-load)    CPU_STRESS_LOAD="$2"; shift 2 ;;
    --cpu-stress-workers) CPU_STRESS_WORKERS="$2"; shift 2 ;;
    -o|--output)          OUTPUT_ROOT="$2"; shift 2 ;;
    --warmup)        WARMUP_SEC="$2"; shift 2 ;;
    --prefault)      PREFAULT_SEC="$2"; shift 2 ;;
    --fault)         FAULT_SEC="$2"; shift 2 ;;
    --recovery)      RECOVERY_SEC="$2"; shift 2 ;;
    --cooldown)      COOLDOWN_SEC="$2"; shift 2 ;;
    --settle)        POST_POLICY_SETTLE_SEC="$2"; shift 2 ;;
    -n|--dry-run)    DRY_RUN=true; shift ;;
    --skip-analyze)  SKIP_ANALYZE=true; shift ;;
    -h|--help)       usage 0 ;;
    *) err "unknown argument: $1 (see --help)" ;;
  esac
done

# Capture CLI-set durations so they can override the scenario config.
CLI_WARMUP="${WARMUP_SEC}"
CLI_PREFAULT="${PREFAULT_SEC}"
CLI_FAULT="${FAULT_SEC}"
CLI_RECOVERY="${RECOVERY_SEC}"
CLI_COOLDOWN="${COOLDOWN_SEC}"

# --------------------------------------------------------------------------- #
# Load scenario config
# --------------------------------------------------------------------------- #

SCENARIO_FILE="${SCRIPT_DIR}/scenarios/${SCENARIO}.conf"
[[ -f "${SCENARIO_FILE}" ]] || err "scenario config not found: ${SCENARIO_FILE}"
# shellcheck source=/dev/null
source "${SCENARIO_FILE}"

# Re-apply CLI overrides (they win over scenario defaults).
WARMUP_SEC="${CLI_WARMUP}"
PREFAULT_SEC="${CLI_PREFAULT}"
FAULT_SEC="${CLI_FAULT}"
RECOVERY_SEC="${CLI_RECOVERY}"
COOLDOWN_SEC="${CLI_COOLDOWN}"

[[ -n "${SCENARIO_FAULT_MANIFEST}" ]] || err "scenario ${SCENARIO} did not set SCENARIO_FAULT_MANIFEST"

# Apply --fault-manifest override if provided. Accepts any of:
#   cartservice-50pct              → resolved to faults/cartservice-50pct.yaml
#   cartservice-50pct.yaml         → resolved to faults/cartservice-50pct.yaml
#   faults/cartservice-50pct.yaml  → used as-is (relative to manifests/online-boutique/)
#   /absolute/path/to/file.yaml    → used as-is
if [[ -n "${FAULT_MANIFEST_OVERRIDE}" ]]; then
  if [[ "${FAULT_MANIFEST_OVERRIDE}" == /* ]]; then
    # absolute path — use directly
    FAULT_MANIFEST_PATH="${FAULT_MANIFEST_OVERRIDE}"
    SCENARIO_FAULT_MANIFEST="${FAULT_MANIFEST_OVERRIDE#${PROTO_DIR}/${FAULT_DIR_REL}/}"
  else
    _fm="${FAULT_MANIFEST_OVERRIDE}"
    [[ "${_fm}" != *.yaml && "${_fm}" != *.yml ]] && _fm="${_fm}.yaml"
    [[ "${_fm}" != */* ]] && _fm="faults/${_fm}"
    SCENARIO_FAULT_MANIFEST="${_fm}"
    FAULT_MANIFEST_PATH="${PROTO_DIR}/${FAULT_DIR_REL}/${SCENARIO_FAULT_MANIFEST}"
  fi
else
  FAULT_MANIFEST_PATH="${PROTO_DIR}/${FAULT_DIR_REL}/${SCENARIO_FAULT_MANIFEST}"
fi
[[ -f "${FAULT_MANIFEST_PATH}" ]] || err "fault manifest not found: ${FAULT_MANIFEST_PATH}"

# Base routing config (mesh-wide retries, no fault). Re-applied to "remove"
# the fault — see service-retries.yaml header for the rationale.
SERVICE_RETRIES_PATH="${PROTO_DIR}/${FAULT_DIR_REL}/service-retries.yaml"
[[ -f "${SERVICE_RETRIES_PATH}" ]] || warn "service-retries.yaml not found at ${SERVICE_RETRIES_PATH} — fault removal will use kubectl delete instead of apply-to-restore"

# CPU-stress template path. Only consulted when --cpu-stress-target is set;
# the runner sed-substitutes the placeholders and pipes the result into
# kubectl apply at fault_start.
CHAOS_CPU_STRESS_TEMPLATE="${PROTO_DIR}/${FAULT_DIR_REL}/chaos/cart-cpu-stress.yaml"
CHAOS_STRESS_RESOURCE="stresschaos/experiment-cpu-stress"

# Validation: --cpu-stress-target requires a non-zero fault window (the
# trigger fires during the fault phase).
if [[ -n "${CPU_STRESS_TARGET}" ]]; then
  (( FAULT_SEC > 0 )) || \
    err "--cpu-stress-target requires --fault > 0 (the trigger fires during the fault window)"
  [[ -f "${CHAOS_CPU_STRESS_TEMPLATE}" ]] || \
    err "chaos template not found: ${CHAOS_CPU_STRESS_TEMPLATE}"
fi

# --------------------------------------------------------------------------- #
# Resolve policies and output directory
# --------------------------------------------------------------------------- #

IFS=',' read -r -a POLICIES <<< "${POLICIES_CSV}"
VALID_POLICIES=("no-control" "circuit-breaker" "envoy-retry-budget" "arolla")
for p in "${POLICIES[@]}"; do
  found=false
  for v in "${VALID_POLICIES[@]}"; do
    [[ "${p}" == "${v}" ]] && found=true && break
  done
  $found || err "unknown policy: ${p}  (valid: ${VALID_POLICIES[*]})"
done

# experiment.json embeds each policy manifest as a parsed structure, which
# requires pyyaml. Check upfront so the run aborts early with a fix instead
# of failing partway through after a long warmup. The check uses the same
# `python3` the heredoc later picks up, which may differ from interactive
# shell python — so a passing `python3 -c "import yaml"` in your terminal
# is not sufficient evidence that the script will see it.
python3 -c "import yaml" >/dev/null 2>&1 || err "pyyaml is required to snapshot policy manifests into experiment.json.
       Install it for the python3 this script invokes:
           python3 -m pip install --user pyyaml
       (then re-run; no other change needed.)"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${OUTPUT_ROOT}/${RUN_TS}"

# Total duration per policy
TOTAL_SEC=$((WARMUP_SEC + PREFAULT_SEC + FAULT_SEC + RECOVERY_SEC + COOLDOWN_SEC))
TOTAL_POLICIES=${#POLICIES[@]}
GRAND_TOTAL_SEC=$((TOTAL_SEC * TOTAL_POLICIES + POST_POLICY_SETTLE_SEC * TOTAL_POLICIES))

# --------------------------------------------------------------------------- #
# Pretty-print plan
# --------------------------------------------------------------------------- #

print_plan() {
  cat <<EOF

================================================================================
Experiment plan
================================================================================
Scenario        : ${SCENARIO_TITLE:-${SCENARIO}}
Fault manifest  : ${FAULT_DIR_REL}/${SCENARIO_FAULT_MANIFEST}
Trigger         : $(if [[ -n "${CPU_STRESS_TARGET}" ]]; then
    echo "chaos-mesh CPU stress (target=${CPU_STRESS_TARGET}, load=${CPU_STRESS_LOAD}%, workers=${CPU_STRESS_WORKERS}, duration=${FAULT_SEC}s)"
  else
    echo "Istio fault manifest"
  fi)
Policies (${TOTAL_POLICIES})   : ${POLICIES[*]}
Client profiles : ${CLIENT_PROFILES:-(all profiles from profiles/)}
Output dir      : ${RUN_DIR}

Per-policy timeline  (total ${TOTAL_SEC}s):
    0s ─────────────────────────── start load
         [warmup       ${WARMUP_SEC}s]
  ${WARMUP_SEC}s ─────────────────────────── baseline begins
         [pre-fault    ${PREFAULT_SEC}s]
  $((WARMUP_SEC + PREFAULT_SEC))s ─────────────────────────── fault injected
         [fault        ${FAULT_SEC}s]
  $((WARMUP_SEC + PREFAULT_SEC + FAULT_SEC))s ─────────────────────────── fault removed
         [recovery     ${RECOVERY_SEC}s]
  $((WARMUP_SEC + PREFAULT_SEC + FAULT_SEC + RECOVERY_SEC))s ─────────────────────────── stop load
         [cooldown     ${COOLDOWN_SEC}s]
  ${TOTAL_SEC}s ─────────────────────────── done

Grand total (all policies): ~${GRAND_TOTAL_SEC}s
  ($(printf "%d min %d sec" $((GRAND_TOTAL_SEC / 60)) $((GRAND_TOTAL_SEC % 60))))

================================================================================
EOF
}

print_plan

if "${DRY_RUN}"; then
  log "dry run — exiting before touching anything"
  exit 0
fi

mkdir -p "${RUN_DIR}"

# Write the top-level experiment descriptor. analyze.py reads this.
#
# Goal: experiment.json should fully describe what was actually run, so that
# a year from now we can replay or interpret a result without re-reading
# every YAML and JSON file the runner touched. That means embedding:
#
#   - the full content of each profile in `client_profiles` (not just names),
#     so future profile edits don't retroactively change what old runs mean
#   - the trigger descriptor (cpu_stress or istio_fault) — already done, kept
#   - the resolved fault manifest path ONLY when the trigger uses one
#   - the resolved policy and profile lists, even when they come from defaults
#
# We delegate the JSON building to Python because nested objects + arrays in
# bash heredocs were a bug source and embedding profile JSON in bash
# string-interpolation would be even worse.

# Resolve the actual list of profiles that will run. If CLIENT_PROFILES is
# empty (= "use all profiles in the dir"), enumerate the directory so the
# experiment.json records the real set, not a misleading empty array.
PROFILE_DIR="${PROTO_DIR}/clients/online-boutique/profiles"
if [[ -n "${CLIENT_PROFILES}" ]]; then
  IFS=',' read -r -a _cp_arr <<< "${CLIENT_PROFILES}"
  RESOLVED_PROFILES=("${_cp_arr[@]}")
else
  RESOLVED_PROFILES=()
  for _f in "${PROFILE_DIR}"/*.json; do
    [[ -f "${_f}" ]] || continue
    RESOLVED_PROFILES+=("$(basename "${_f}" .json)")
  done
fi

# Hand off to Python for the JSON build. Args are passed via env vars to
# avoid quoting nightmares with shell array → python list translation.
export _RUN_DIR="${RUN_DIR}"
export _SCENARIO="${SCENARIO}"
export _SCENARIO_TITLE="${SCENARIO_TITLE}"
export _SCENARIO_FAULT_MANIFEST="${SCENARIO_FAULT_MANIFEST}"
export _PROFILE_DIR="${PROFILE_DIR}"
export _POLICY_DIR="${PROTO_DIR}/manifests/online-boutique/policies"
export _RUN_TS="${RUN_TS}"
export _MASTER_HOST="${MASTER_HOST}"
export _CLIENT_HOST="${CLIENT_HOST}"
export _WARMUP_SEC="${WARMUP_SEC}"
export _PREFAULT_SEC="${PREFAULT_SEC}"
export _FAULT_SEC="${FAULT_SEC}"
export _RECOVERY_SEC="${RECOVERY_SEC}"
export _COOLDOWN_SEC="${COOLDOWN_SEC}"
export _CPU_STRESS_TARGET="${CPU_STRESS_TARGET}"
export _CPU_STRESS_LOAD="${CPU_STRESS_LOAD}"
export _CPU_STRESS_WORKERS="${CPU_STRESS_WORKERS}"
export _POLICIES_CSV
_POLICIES_CSV="$(IFS=,; echo "${POLICIES[*]}")"
export _RESOLVED_PROFILES_CSV
_RESOLVED_PROFILES_CSV="$(IFS=,; echo "${RESOLVED_PROFILES[*]}")"
export _WORKER_HOSTS_CSV
_WORKER_HOSTS_CSV="$(IFS=,; echo "${WORKER_HOSTS[*]}")"

python3 - <<'PYEOF'
import json, os
from pathlib import Path

policies = [p for p in os.environ["_POLICIES_CSV"].split(",") if p]
profile_names = [p for p in os.environ["_RESOLVED_PROFILES_CSV"].split(",") if p]
worker_hosts = [w for w in os.environ["_WORKER_HOSTS_CSV"].split(",") if w]

# Embed each profile's full JSON content under client_profiles[*].spec.
# If a profile file has gone missing (unlikely but possible), record an
# error placeholder rather than crashing.
profile_dir = Path(os.environ["_PROFILE_DIR"])
client_profiles = []
for name in profile_names:
    p = profile_dir / f"{name}.json"
    entry = {"name": name, "source": str(p)}
    try:
        entry["spec"] = json.loads(p.read_text())
    except FileNotFoundError:
        entry["error"] = f"profile file not found: {p}"
    except json.JSONDecodeError as e:
        entry["error"] = f"profile JSON parse error: {e}"
    client_profiles.append(entry)

# Snapshot each policy manifest as a parsed structure (comments stripped,
# JSON-friendly). For multi-document manifests we keep the list; for the
# common single-doc case we unwrap. Templated bits like __MASTER_IP__ are
# recorded literally — the substituted value is whatever deploy-policy.sh
# resolved at apply time, and the master IP is recorded under cluster.master
# below so the substitution is reconstructible.
#
# pyyaml is required (validated upfront in run-experiment.sh, so this
# import never fails by the time we get here).
import yaml  # noqa: E402

policy_dir = Path(os.environ["_POLICY_DIR"])
policies_spec = []
for name in policies:
    p = policy_dir / f"{name}.yaml"
    entry = {"name": name, "source": str(p)}
    try:
        docs = [d for d in yaml.safe_load_all(p.read_text()) if d is not None]
        entry["manifest"] = docs[0] if len(docs) == 1 else docs
    except FileNotFoundError:
        entry["error"] = f"policy manifest not found: {p}"
    except yaml.YAMLError as e:
        entry["error"] = f"policy manifest parse error: {e}"
    policies_spec.append(entry)

# Trigger descriptor — mutually exclusive: cpu_stress vs istio_fault.
cpu_target = os.environ["_CPU_STRESS_TARGET"]
if cpu_target:
    trigger = {
        "kind": "cpu_stress",
        "target": cpu_target,
        "load_pct": int(os.environ["_CPU_STRESS_LOAD"]),
        "workers": int(os.environ["_CPU_STRESS_WORKERS"]),
        "duration_sec": int(os.environ["_FAULT_SEC"]),
    }
else:
    trigger = {
        "kind": "istio_fault",
        "manifest": os.environ["_SCENARIO_FAULT_MANIFEST"],
        "duration_sec": int(os.environ["_FAULT_SEC"]),
    }

doc = {
    "scenario": os.environ["_SCENARIO"],
    "scenario_title": os.environ["_SCENARIO_TITLE"],
    # `policies` stays as a flat list of names for backwards compatibility
    # with analyze.py and any old tooling that iterates it. The structured
    # snapshot lives in `policies_spec` (mirroring `client_profiles`) and
    # records the exact manifest text that was on disk at run time.
    "policies": policies,
    "policies_spec": policies_spec,
    "client_profiles": client_profiles,
    "warmup_sec": int(os.environ["_WARMUP_SEC"]),
    "prefault_sec": int(os.environ["_PREFAULT_SEC"]),
    "fault_sec": int(os.environ["_FAULT_SEC"]),
    "recovery_sec": int(os.environ["_RECOVERY_SEC"]),
    "cooldown_sec": int(os.environ["_COOLDOWN_SEC"]),
    "trigger": trigger,
    "run_ts": os.environ["_RUN_TS"],
    "cluster": {
        "master": os.environ["_MASTER_HOST"],
        "workers": worker_hosts,
        "client": os.environ["_CLIENT_HOST"],
    },
}

# Only emit scenario_fault_manifest at the top level when an istio_fault
# trigger actually applies it. For cpu_stress runs the field is misleading
# (the manifest is never applied) and would confuse future readers.
if trigger["kind"] == "istio_fault":
    doc["scenario_fault_manifest"] = os.environ["_SCENARIO_FAULT_MANIFEST"]

out_path = Path(os.environ["_RUN_DIR"]) / "experiment.json"
out_path.write_text(json.dumps(doc, indent=2) + "\n")
PYEOF

log "wrote ${RUN_DIR}/experiment.json"

# --------------------------------------------------------------------------- #
# Cleanup trap
# --------------------------------------------------------------------------- #

cleanup() {
  local code=$?
  warn "cleanup: restoring base routing + stopping clients"
  if [[ -f "${SERVICE_RETRIES_PATH}" ]]; then
    kubectl apply -f "${SERVICE_RETRIES_PATH}" >/dev/null 2>&1 || true
  else
    kubectl delete --ignore-not-found -f "${FAULT_MANIFEST_PATH}" >/dev/null 2>&1 || true
  fi
  # Best-effort: clean up any lingering chaos-mesh CPU-stress resource.
  # No-op if chaos-mesh isn't installed or the resource doesn't exist.
  kubectl delete -n "${NAMESPACE}" "${CHAOS_STRESS_RESOURCE}" --ignore-not-found >/dev/null 2>&1 || true
  ssh_client "if [[ -f ${REMOTE_PID_FILE} ]]; then kill \$(cat ${REMOTE_PID_FILE}) 2>/dev/null || true; rm -f ${REMOTE_PID_FILE}; fi" >/dev/null 2>&1 || true
  exit "${code}"
}
trap cleanup INT TERM

# --------------------------------------------------------------------------- #
# Phase helpers
# --------------------------------------------------------------------------- #

wait_until() {
  # Sleep in 1-second chunks until the wall-clock reaches $1. Avoids drift
  # from cumulative `sleep N` calls, and lets Ctrl+C respond promptly.
  local target="$1"
  while (( $(date +%s) < target )); do
    sleep 1
  done
}

now() { date +%s; }

# Services whose sidecar stats we dump each run. Declared at file scope so
# both the pre-run snapshot and the post-run dump share the same list.
ONLINE_BOUTIQUE_SERVICES=(
  frontend
  checkoutservice
  cartservice
  productcatalogservice
  recommendationservice
  paymentservice
  shippingservice
  currencyservice
  emailservice
  adservice
)

# dump_sidecar_stats <output_dir> <suffix>
#
# For each Online Boutique service, fetch /stats and /stats?format=prometheus
# from its istio-proxy sidecar, writing to
#   <output_dir>/<svc><suffix>.stats
#   <output_dir>/<svc><suffix>.prom
#
# `<suffix>` is "" for the post-run dump and ".pre" for the pre-run snapshot.
# Called at both ends of an experiment so analyze.py can diff bucket counts
# and attribute latency histograms to the current run only.
dump_sidecar_stats() {
  local out_dir="$1"
  local suffix="${2:-}"
  mkdir -p "${out_dir}"
  for svc in "${ONLINE_BOUTIQUE_SERVICES[@]}"; do
    local pod
    pod="$(kubectl -n "${NAMESPACE}" get pod -l app="${svc}" \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [[ -z "${pod}" ]]; then
      warn "no pod for ${svc}, skipping"
      continue
    fi
    kubectl -n "${NAMESPACE}" exec "${pod}" -c istio-proxy -- \
      curl -s 'localhost:15000/stats' \
      > "${out_dir}/${svc}${suffix}.stats" 2>/dev/null || \
      warn "could not dump counters for ${svc}${suffix}"
    kubectl -n "${NAMESPACE}" exec "${pod}" -c istio-proxy -- \
      curl -s 'localhost:15000/stats?format=prometheus' \
      > "${out_dir}/${svc}${suffix}.prom" 2>/dev/null || \
      warn "could not dump prometheus stats for ${svc}${suffix}"
  done
}

# --------------------------------------------------------------------------- #
# Single-policy run
# --------------------------------------------------------------------------- #

run_single() {
  local policy="$1"
  local out_dir="${RUN_DIR}/${policy}"
  local client_metrics_dir="${out_dir}/client-metrics"
  local sidecar_stats_dir="${out_dir}/sidecar-stats"

  mkdir -p "${client_metrics_dir}" "${sidecar_stats_dir}"

  phase "================ policy: ${policy} ================"

  # ---- Pre-run cleanup (make the run reproducible) ----
  log "pre-run: restoring base routing, clearing remote metrics"
  if [[ -f "${SERVICE_RETRIES_PATH}" ]]; then
    kubectl apply -f "${SERVICE_RETRIES_PATH}" >/dev/null 2>&1 || true
  else
    kubectl delete --ignore-not-found -f "${FAULT_MANIFEST_PATH}" >/dev/null 2>&1 || true
  fi
  # Best-effort: drop any lingering chaos-mesh CPU-stress from a prior run.
  kubectl delete -n "${NAMESPACE}" "${CHAOS_STRESS_RESOURCE}" --ignore-not-found >/dev/null 2>&1 || true
  ssh_client "rm -rf ${REMOTE_METRICS_DIR} ${REMOTE_PID_FILE} ${REMOTE_LOG_FILE}; mkdir -p ${REMOTE_METRICS_DIR}"

  # ---- Switch policy ----
  log "switching policy → ${policy}"
  "${PROTO_DIR}/deploy-policy.sh" switch "${policy}"
  log "waiting ${POST_POLICY_SETTLE_SEC}s for xDS push to settle"
  sleep "${POST_POLICY_SETTLE_SEC}"

  # ---- Snapshot sidecar bucket state BEFORE any new traffic ----
  # analyze.py's latency CDF subtracts this baseline from the post-run dump
  # so per-service histograms are scoped to this experiment's traffic only.
  log "[${policy}] snapshotting sidecar bucket state (pre.prom)"
  dump_sidecar_stats "${sidecar_stats_dir}" ".pre"

  # ---- Define absolute phase boundaries (drift-free scheduling) ----
  local t0 t_warmup_end t_prefault_end t_fault_start t_fault_end t_recovery_end t_cooldown_end
  t0="$(now)"
  t_warmup_end=$((t0 + WARMUP_SEC))
  t_prefault_end=$((t_warmup_end + PREFAULT_SEC))
  t_fault_start="${t_prefault_end}"
  t_fault_end=$((t_fault_start + FAULT_SEC))
  t_recovery_end=$((t_fault_end + RECOVERY_SEC))
  t_cooldown_end=$((t_recovery_end + COOLDOWN_SEC))

  # ---- Start load ----
  if [[ -n "${CLIENT_PROFILES}" ]]; then
    phase "[${policy}] start load at t=${t0} (profiles: ${CLIENT_PROFILES})"
    PROFILES="${CLIENT_PROFILES}" \
      "${PROTO_DIR}/clients/online-boutique/run-clients.sh" start
  else
    phase "[${policy}] start load at t=${t0} (all profiles)"
    "${PROTO_DIR}/clients/online-boutique/run-clients.sh" start
  fi

  # ---- Warmup ----
  log "[${policy}] warmup → t+${WARMUP_SEC}s"
  wait_until "${t_warmup_end}"

  # ---- Pre-fault baseline ----
  log "[${policy}] pre-fault baseline → t+$((WARMUP_SEC + PREFAULT_SEC))s"
  wait_until "${t_prefault_end}"

  # ---- Fault window (skipped entirely when FAULT_SEC=0) ----
  # When --fault 0 is passed we're running a clean baseline with no fault
  # injection at all — the "fault" and "recovery" labels become purely
  # nominal phase markers in the timeline, but nothing ever gets perturbed.
  # This is the recommended way to measure a healthy baseline.
  #
  # When --cpu-stress-target is set, the trigger is a chaos-mesh StressChaos
  # applied to one pod of that workload, INSTEAD OF the Istio fault manifest.
  # This is the OSDI '22 metastability methodology — a transient CPU
  # restriction on a stateful component, applied for FAULT_SEC seconds.
  if (( FAULT_SEC > 0 )); then
    if [[ -n "${CPU_STRESS_TARGET}" ]]; then
      # ---- Trigger A: chaos-mesh CPU stress ----
      phase "[${policy}] applying CPU stress: target=${CPU_STRESS_TARGET} load=${CPU_STRESS_LOAD}% workers=${CPU_STRESS_WORKERS} duration=${FAULT_SEC}s"
      sed -e "s|__TARGET__|${CPU_STRESS_TARGET}|g" \
          -e "s|__LOAD__|${CPU_STRESS_LOAD}|g" \
          -e "s|__WORKERS__|${CPU_STRESS_WORKERS}|g" \
          -e "s|__DURATION__|${FAULT_SEC}|g" \
          "${CHAOS_CPU_STRESS_TEMPLATE}" \
        | kubectl apply -f -
      wait_until "${t_fault_end}"

      phase "[${policy}] removing CPU stress (chaos-mesh auto-cleanup is a backstop)"
      kubectl delete -n "${NAMESPACE}" "${CHAOS_STRESS_RESOURCE}" --ignore-not-found >/dev/null
    else
      # ---- Trigger B: Istio fault manifest ----
      phase "[${policy}] inject fault ($(basename "${FAULT_MANIFEST_PATH}"))"
      kubectl apply -f "${FAULT_MANIFEST_PATH}" >/dev/null
      wait_until "${t_fault_end}"

      # ---- Remove fault ----
      # Apply the base routing config (chain retries, no abort/timeout)
      # which has the same VirtualService names as the fault manifest, so
      # kubectl apply replaces the faulted entries in-place. Falling back
      # to `delete` would also remove the chain-retry config and produce
      # an unrealistic recovery.
      phase "[${policy}] remove fault (restore base routing)"
      if [[ -f "${SERVICE_RETRIES_PATH}" ]]; then
        kubectl apply -f "${SERVICE_RETRIES_PATH}" >/dev/null
      else
        kubectl delete -f "${FAULT_MANIFEST_PATH}" >/dev/null
      fi
    fi
  else
    log "[${policy}] fault window = 0s, skipping trigger (clean baseline run)"
  fi

  # ---- Recovery ----
  log "[${policy}] recovery → t+$((WARMUP_SEC + PREFAULT_SEC + FAULT_SEC + RECOVERY_SEC))s"
  wait_until "${t_recovery_end}"

  # ---- Stop load ----
  phase "[${policy}] stop load"
  "${PROTO_DIR}/clients/online-boutique/run-clients.sh" stop >/dev/null || true

  # ---- Cooldown (drain metrics) ----
  log "[${policy}] cooldown → t+${TOTAL_SEC}s"
  wait_until "${t_cooldown_end}"

  # ---- Write timeline ----
  cat > "${out_dir}/timeline.json" <<EOF
{
  "policy": "${policy}",
  "t_start": ${t0},
  "t_warmup_end": ${t_warmup_end},
  "t_prefault_end": ${t_prefault_end},
  "t_fault_start": ${t_fault_start},
  "t_fault_end": ${t_fault_end},
  "t_recovery_end": ${t_recovery_end},
  "t_cooldown_end": ${t_cooldown_end}
}
EOF

  # ---- Fetch client metrics ----
  log "[${policy}] fetching client metrics"
  scp_from_client "${REMOTE_METRICS_DIR}/." "${client_metrics_dir}/" || \
    warn "failed to fetch client metrics (dir may be empty)"
  scp_from_client "${REMOTE_LOG_FILE}" "${out_dir}/traffic_gen.log" >/dev/null 2>&1 || true

  # ---- Dump sidecar stats post-run ----
  # Two formats (see dump_sidecar_stats at file scope):
  #   <svc>.stats — plain text counters + gauges (easy to grep)
  #   <svc>.prom  — Prometheus format w/ histogram buckets for latency CDFs
  # analyze.py pairs each <svc>.prom with its <svc>.pre.prom sibling so
  # per-service bucket counts are diffed and scoped to this run only.
  log "[${policy}] dumping sidecar stats (counters + histograms)"
  dump_sidecar_stats "${sidecar_stats_dir}" ""

  phase "[${policy}] done → ${out_dir}"
}

# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

log "starting experiment: ${#POLICIES[@]} policies × ${TOTAL_SEC}s each"
T_EXPERIMENT_START="$(now)"

for policy in "${POLICIES[@]}"; do
  run_single "${policy}"
done

T_EXPERIMENT_END="$(now)"
ELAPSED=$((T_EXPERIMENT_END - T_EXPERIMENT_START))

phase "all policies complete in ${ELAPSED}s → ${RUN_DIR}"

# --------------------------------------------------------------------------- #
# Analyze
# --------------------------------------------------------------------------- #

if "${SKIP_ANALYZE}"; then
  log "skipping analyze.py (--skip-analyze)"
else
  if command -v python3 >/dev/null; then
    log "running analyzer → ${RUN_DIR}/plots/"
    python3 "${SCRIPT_DIR}/analyze.py" "${RUN_DIR}" || warn "analyzer failed"
  else
    warn "python3 not found on PATH — skipping analyzer"
  fi
fi

log "DONE"
