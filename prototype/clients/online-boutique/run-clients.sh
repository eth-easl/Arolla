#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
source "${PROTO_DIR}/k8s-config.sh"

APP_NS="online-boutique"
APP_HOST="boutique.example.com"
APP_GATEWAY_NAME="boutique-gateway"

REMOTE_BASE="${REMOTE_BASE:-/tmp/online-boutique-clients}"
REMOTE_PID_FILE="${REMOTE_BASE}/traffic_gen.pid"
REMOTE_LOG_FILE="${REMOTE_BASE}/traffic_gen.log"
REMOTE_METRICS_DIR="${REMOTE_BASE}/metrics"

SSH="ssh ${SSH_OPTS} ${SSH_USER}@${CLIENT_HOST}"
MASTER_SSH="ssh ${SSH_OPTS} ${SSH_USER}@${MASTER_HOST}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <start|stop|status|logs|fetch-metrics>

Runs external retry-study clients on CLIENT_HOST (from prototype/k8s-config.sh).

Env overrides:
  PROFILES     Comma-separated subset of profile names (default: all)
  REMOTE_BASE  Remote workspace dir on CLIENT_HOST (default: ${REMOTE_BASE})
EOF
}

get_gateway_url() {
  local node_port
  node_port=$(${MASTER_SSH} "kubectl -n ${APP_NS} get svc \
    -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
    -o jsonpath='{.items[0].spec.ports[?(@.name==\"http\")].nodePort}'" 2>/dev/null || true)
  node_port="$(echo "${node_port}" | tr -d '[:space:]')"
  if [[ -z "${node_port}" ]]; then
    echo "Error: could not resolve gateway NodePort for ${APP_GATEWAY_NAME} in ${APP_NS}" >&2
    return 1
  fi
  echo "http://${MASTER_HOST}:${node_port}"
}

remote_mkdir() {
  ${SSH} "mkdir -p '${REMOTE_BASE}/profiles' '${REMOTE_METRICS_DIR}'"
}

upload_files() {
  remote_mkdir
  scp ${SSH_OPTS} "${SCRIPT_DIR}/traffic_gen.py" "${SSH_USER}@${CLIENT_HOST}:${REMOTE_BASE}/traffic_gen.py" >/dev/null
  scp ${SSH_OPTS} "${SCRIPT_DIR}/profiles/"*.json "${SSH_USER}@${CLIENT_HOST}:${REMOTE_BASE}/profiles/" >/dev/null
}

start_clients() {
  local target_url profiles_arg
  target_url="$(get_gateway_url)"
  profiles_arg="${PROFILES:-}"

  echo "[info] CLIENT_HOST: ${CLIENT_HOST}"
  echo "[info] Gateway URL:  ${target_url}"
  [[ -n "${profiles_arg}" ]] && echo "[info] Profiles:     ${profiles_arg}"

  upload_files

  ${SSH} "bash -lc '
    set -euo pipefail
    mkdir -p \"${REMOTE_BASE}\" \"${REMOTE_METRICS_DIR}\"
    if [[ -f \"${REMOTE_PID_FILE}\" ]] && kill -0 \$(cat \"${REMOTE_PID_FILE}\") 2>/dev/null; then
      echo \"already running (pid=\$(cat \"${REMOTE_PID_FILE}\"))\"
      exit 0
    fi
    rm -f \"${REMOTE_PID_FILE}\"
    if ! python3 -c \"import aiohttp\" >/dev/null 2>&1; then
      echo \"Installing aiohttp (user site) on CLIENT_HOST...\"
      python3 -m pip install --user aiohttp >/dev/null
    fi
    nohup python3 \"${REMOTE_BASE}/traffic_gen.py\" \
      --target-base-url \"${target_url}\" \
      --host-header \"${APP_HOST}\" \
      --profile-dir \"${REMOTE_BASE}/profiles\" \
      ${profiles_arg:+--profiles \"${profiles_arg}\"} \
      --output-dir \"${REMOTE_METRICS_DIR}\" \
      > \"${REMOTE_LOG_FILE}\" 2>&1 < /dev/null &
    echo \$! > \"${REMOTE_PID_FILE}\"
    echo \"started pid=\$(cat \"${REMOTE_PID_FILE}\")\"
  '"
}

stop_clients() {
  ${SSH} "bash -lc '
    set -euo pipefail
    if [[ ! -f \"${REMOTE_PID_FILE}\" ]]; then
      echo \"not running (no pid file)\"
      exit 0
    fi
    pid=\$(cat \"${REMOTE_PID_FILE}\")
    if kill -0 \"\$pid\" 2>/dev/null; then
      kill \"\$pid\" || true
      sleep 1
      kill -9 \"\$pid\" 2>/dev/null || true
      echo \"stopped pid=\$pid\"
    else
      echo \"stale pid file (pid=\$pid)\"
    fi
    rm -f \"${REMOTE_PID_FILE}\"
  '"
}

status_clients() {
  local target_url
  target_url="$(get_gateway_url || true)"
  echo "[info] CLIENT_HOST: ${CLIENT_HOST}"
  [[ -n "${target_url}" ]] && echo "[info] Gateway URL:  ${target_url}"
  ${SSH} "bash -lc '
    set -euo pipefail
    echo \"remote_base=${REMOTE_BASE}\"
    if [[ -f \"${REMOTE_PID_FILE}\" ]]; then
      pid=\$(cat \"${REMOTE_PID_FILE}\")
      if kill -0 \"\$pid\" 2>/dev/null; then
        echo \"status=running pid=\$pid\"
      else
        echo \"status=stale-pid pid=\$pid\"
      fi
    else
      echo \"status=stopped\"
    fi
    ls -lh \"${REMOTE_LOG_FILE}\" 2>/dev/null || true
    ls -lh \"${REMOTE_METRICS_DIR}\" 2>/dev/null || true
  '"
}

logs_clients() {
  ${SSH} "bash -lc 'tail -n 80 \"${REMOTE_LOG_FILE}\" 2>/dev/null || echo \"(no log file)\"'"
}

fetch_metrics() {
  local out_dir="${SCRIPT_DIR}/outputs/$(date +%Y%m%d_%H%M%S)"
  mkdir -p "${out_dir}"
  scp ${SSH_OPTS} "${SSH_USER}@${CLIENT_HOST}:${REMOTE_METRICS_DIR}/*" "${out_dir}/" 2>/dev/null || true
  echo "[info] Fetched metrics to ${out_dir}"
}

cmd="${1:-}"
case "${cmd}" in
  start) start_clients ;;
  stop) stop_clients ;;
  status) status_clients ;;
  logs) logs_clients ;;
  fetch-metrics) fetch_metrics ;;
  ""|-h|--help|help) usage ;;
  *) echo "Unknown command: ${cmd}" >&2; usage; exit 1 ;;
esac
