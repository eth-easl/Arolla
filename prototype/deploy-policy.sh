#!/usr/bin/env bash
# ============================================================================
# deploy-policy.sh — apply / remove / switch server-side retry policies
#                    and serve the Arolla Wasm binary to sidecars
# ============================================================================
#
# Subcommands:
#
#   build-wasm            Build the Arolla Wasm filter locally via cargo.
#                         Requires `rustup` + the wasm32-wasip1 target.
#
#   upload-wasm           scp the built .wasm to the master node.
#
#   serve-wasm [port]     Start `python3 -m http.server` on the master in
#                         background, serving the wasm directory.
#                         Default port: 8000.
#
#   stop-wasm             Kill any http.server processes on the master.
#
#   apply   <policy>      Apply a policy manifest (see POLICIES below).
#                         For `arolla`, substitutes __MASTER_IP__ at apply time.
#
#   remove  <policy>      Delete a policy manifest.
#
#   switch  <policy>      Remove all policies, then apply <policy>.
#                         Use `switch none-of-the-above` to just clean up.
#
#   status                Print which policies are currently applied.
#
# Policies:
#   arolla             — Arolla WasmPlugin (paper §4)
#   no-control         — reset DestinationRule, no retry gating
#   circuit-breaker    — Envoy outlier detection (paper §6.1 baseline 2)
#   envoy-retry-budget — Envoy retry budget 20% (paper §6.1 baseline 3)
#
# Prereqs:
#   * `kubectl` on PATH, configured for the target cluster
#   * k8s-config.sh populated (for SSH to master)
#   * Arolla wasm binary built locally under arolla-filter/target/...
#
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=k8s-config.sh
source "${SCRIPT_DIR}/k8s-config.sh"

NAMESPACE="online-boutique"
MANIFEST_DIR="${SCRIPT_DIR}/manifests/online-boutique/policies"
FAULT_DIR="${SCRIPT_DIR}/manifests/online-boutique/faults"
FILTER_DIR="${SCRIPT_DIR}/arolla-filter"
WASM_PATH="${FILTER_DIR}/target/wasm32-wasip1/release/arolla_filter.wasm"
# Emulab homes live under /users, not /home. Override via env var if your
# cluster uses a different layout.
REMOTE_WASM_DIR="${REMOTE_WASM_DIR:-/users/${SSH_USER}/arolla-wasm}"
REMOTE_WASM_NAME="arolla_filter.wasm"
WASM_PORT_DEFAULT=8000

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

log()  { printf '\033[1;34m[deploy-policy]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[deploy-policy]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[deploy-policy]\033[0m %s\n' "$*" >&2; exit 1; }

ssh_master() {
  local key_opt=""
  [[ -n "${SSH_KEY}" ]] && key_opt="-i ${SSH_KEY}"
  # shellcheck disable=SC2086
  ssh ${SSH_OPTS} ${key_opt} "${SSH_USER}@${MASTER_HOST}" "$@"
}

scp_to_master() {
  local src="$1" dst="$2"
  local key_opt=""
  [[ -n "${SSH_KEY}" ]] && key_opt="-i ${SSH_KEY}"
  # shellcheck disable=SC2086
  scp ${SSH_OPTS} ${key_opt} "${src}" "${SSH_USER}@${MASTER_HOST}:${dst}"
}

resolve_master_ip() {
  # Use the explicit MASTER_IP from k8s-config.sh if present, otherwise ask
  # the master for the first address on its experimental network.
  if [[ -n "${MASTER_IP}" ]]; then
    echo "${MASTER_IP}"
    return
  fi
  local ip
  ip="$(ssh_master "hostname -I | awk '{print \$1}'")" || err "failed to resolve master IP via SSH"
  ip="${ip//[[:space:]]/}"
  [[ -z "${ip}" ]] && err "master returned empty IP"
  echo "${ip}"
}

# --------------------------------------------------------------------------- #
# subcommands — wasm binary lifecycle
# --------------------------------------------------------------------------- #

cmd_build_wasm() {
  command -v cargo >/dev/null || err "cargo not on PATH — install via rustup"
  log "building arolla wasm filter (release)"
  (cd "${FILTER_DIR}" && cargo build --target wasm32-wasip1 --release)
  [[ -f "${WASM_PATH}" ]] || err "build produced no artifact at ${WASM_PATH}"
  log "built: ${WASM_PATH} ($(du -h "${WASM_PATH}" | cut -f1))"
}

cmd_upload_wasm() {
  [[ -f "${WASM_PATH}" ]] || err "wasm not built — run: $0 build-wasm"
  log "preparing remote directory ${REMOTE_WASM_DIR} on ${MASTER_HOST}"
  ssh_master "mkdir -p ${REMOTE_WASM_DIR}"
  log "uploading ${WASM_PATH} → ${MASTER_HOST}:${REMOTE_WASM_DIR}/${REMOTE_WASM_NAME}"
  scp_to_master "${WASM_PATH}" "${REMOTE_WASM_DIR}/${REMOTE_WASM_NAME}"
  log "uploaded"
}

cmd_serve_wasm() {
  local port="${1:-${WASM_PORT_DEFAULT}}"
  log "starting http.server on ${MASTER_HOST}:${port} (serving ${REMOTE_WASM_DIR})"
  # Use nohup + setsid so the server survives the SSH disconnect.
  ssh_master "
    cd ${REMOTE_WASM_DIR} && \
    (pgrep -f 'http.server ${port}' >/dev/null && echo 'already running' && exit 0); \
    nohup setsid python3 -m http.server ${port} \
        >/tmp/arolla-wasm-http.log 2>&1 </dev/null & \
    disown; \
    sleep 1; \
    pgrep -f 'http.server ${port}' >/dev/null && echo 'started' || (echo 'FAILED to start'; tail /tmp/arolla-wasm-http.log; exit 1)
  "
  local ip
  ip="$(resolve_master_ip)"
  log "serving at http://${ip}:${port}/${REMOTE_WASM_NAME}"
}

cmd_stop_wasm() {
  log "stopping http.server on master"
  ssh_master "pkill -f 'http.server' || true"
}

# --------------------------------------------------------------------------- #
# subcommands — policy lifecycle
# --------------------------------------------------------------------------- #

POLICY_FILES=(
  "arolla:arolla.yaml"
  "no-control:no-control.yaml"
  "circuit-breaker:circuit-breaker.yaml"
  "envoy-retry-budget:envoy-retry-budget.yaml"
)

policy_file() {
  local name="$1"
  for entry in "${POLICY_FILES[@]}"; do
    [[ "${entry%%:*}" == "${name}" ]] && echo "${MANIFEST_DIR}/${entry##*:}" && return
  done
  return 1
}

render_arolla() {
  # stdout: arolla.yaml with __MASTER_IP__ substituted
  local ip
  ip="$(resolve_master_ip)"
  [[ -z "${ip}" ]] && err "empty MASTER_IP"
  sed "s/__MASTER_IP__/${ip}/g" "${MANIFEST_DIR}/arolla.yaml"
}

render_arolla_gateway() {
  # stdout: arolla-gateway.yaml with __MASTER_IP__ substituted
  local ip
  ip="$(resolve_master_ip)"
  [[ -z "${ip}" ]] && err "empty MASTER_IP"
  sed "s/__MASTER_IP__/${ip}/g" "${MANIFEST_DIR}/arolla-gateway.yaml"
}

cmd_apply() {
  local name="${1:-}"
  [[ -z "${name}" ]] && err "usage: $0 apply <policy>"
  local file
  file="$(policy_file "${name}")" || err "unknown policy: ${name}"

  log "applying policy: ${name} (${file})"
  if [[ "${name}" == "arolla" ]]; then
    render_arolla | kubectl apply -f -
    render_arolla_gateway | kubectl apply -f -
  else
    kubectl apply -f "${file}"
  fi
}

cmd_remove() {
  local name="${1:-}"
  [[ -z "${name}" ]] && err "usage: $0 remove <policy>"
  local file
  file="$(policy_file "${name}")" || err "unknown policy: ${name}"

  log "removing policy: ${name}"
  if [[ "${name}" == "arolla" ]]; then
    render_arolla | kubectl delete --ignore-not-found -f -
    render_arolla_gateway | kubectl delete --ignore-not-found -f -
  else
    kubectl delete --ignore-not-found -f "${file}"
  fi
}

cmd_switch() {
  local name="${1:-}"
  [[ -z "${name}" ]] && err "usage: $0 switch <policy>"
  log "switching to policy: ${name}"
  # Tear down everything else first so we don't leave a previous policy active.
  for entry in "${POLICY_FILES[@]}"; do
    local other="${entry%%:*}"
    [[ "${other}" == "${name}" ]] && continue
    cmd_remove "${other}" >/dev/null 2>&1 || true
  done
  # Special case: `switch none` just cleans up without applying anything new.
  [[ "${name}" == "none" ]] && { log "all policies removed"; return; }
  cmd_apply "${name}"
}

cmd_status() {
  log "installed policies in ns/${NAMESPACE}:"
  kubectl -n "${NAMESPACE}" get wasmplugin,destinationrule,envoyfilter 2>/dev/null | \
    awk '/arolla|baseline/ || NR==1'
}

# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

main() {
  local cmd="${1:-}"
  [[ -z "${cmd}" ]] && usage
  shift || true

  case "${cmd}" in
    build-wasm)   cmd_build_wasm "$@" ;;
    upload-wasm)  cmd_upload_wasm "$@" ;;
    serve-wasm)   cmd_serve_wasm "$@" ;;
    stop-wasm)    cmd_stop_wasm "$@" ;;
    apply)        cmd_apply "$@" ;;
    remove)       cmd_remove "$@" ;;
    switch)       cmd_switch "$@" ;;
    status)       cmd_status "$@" ;;
    -h|--help|help) usage ;;
    *) err "unknown subcommand: ${cmd}" ;;
  esac
}

main "$@"
