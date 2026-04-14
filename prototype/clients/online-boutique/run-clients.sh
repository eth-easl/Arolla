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
REMOTE_METRICS_DIR="${REMOTE_BASE}/metrics"

# Number of loader processes to launch on CLIENT_HOST. Each owns 1/N of the
# offered load, writes its own client_attempts.shardK.csv, and logs to its
# own traffic_gen.shardK.log. Sharding lets us push beyond the single-core
# GIL ceiling that caps a single Python loader at ~1000 rps.
NUM_LOADERS="${NUM_LOADERS:-1}"

SSH="ssh ${SSH_OPTS} ${SSH_USER}@${CLIENT_HOST}"
MASTER_SSH="ssh ${SSH_OPTS} ${SSH_USER}@${MASTER_HOST}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <start|stop|status|logs|fetch-metrics>

Runs external retry-study clients on CLIENT_HOST (from prototype/k8s-config.sh).

Env overrides:
  PROFILES     Comma-separated subset of profile names (default: all)
  NUM_LOADERS  Number of parallel loader processes (default: 1).
               Each gets 1/N of the offered load and writes its own
               client_attempts.shardK.csv. Use N>1 to push past the
               ~1000 rps single-process Python ceiling.
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

  echo "[info] CLIENT_HOST:  ${CLIENT_HOST}"
  echo "[info] Gateway URL:  ${target_url}"
  echo "[info] NUM_LOADERS:  ${NUM_LOADERS}"
  [[ -n "${profiles_arg}" ]] && echo "[info] Profiles:     ${profiles_arg}"

  upload_files

  ${SSH} "bash -lc '
    set -euo pipefail
    mkdir -p \"${REMOTE_BASE}\" \"${REMOTE_METRICS_DIR}\"

    # Refuse to start if any prior shard is still alive — a stale loader
    # would double the offered load and contaminate the run.
    if [[ -f \"${REMOTE_PID_FILE}\" ]]; then
      while read -r p; do
        [[ -z \"\$p\" ]] && continue
        if kill -0 \"\$p\" 2>/dev/null; then
          echo \"already running (pid=\$p) — run stop first\"
          exit 0
        fi
      done < \"${REMOTE_PID_FILE}\"
    fi

    # Raise fd limit so open-loop profiles can sustain thousands of
    # concurrent sockets without EMFILE (default 1024 is too low).
    ulimit -n 65536 2>/dev/null || ulimit -n 8192 2>/dev/null || true

    # Fresh run: clear stale pid + log files.
    rm -f \"${REMOTE_PID_FILE}\" \"${REMOTE_BASE}\"/traffic_gen.shard*.log \"${REMOTE_BASE}\"/traffic_gen.log

    # Launch NUM_LOADERS shards. Each owns 1/N of the offered load and
    # writes to its own log + CSV. We append to the pid file (one PID
    # per line) so stop_clients can kill them all later.
    : > \"${REMOTE_PID_FILE}\"
    for ((i=0; i<${NUM_LOADERS}; i++)); do
      log_i=\"${REMOTE_BASE}/traffic_gen.shard\$i.log\"
      nohup python3 \"${REMOTE_BASE}/traffic_gen.py\" \
        --target-base-url \"${target_url}\" \
        --host-header \"${APP_HOST}\" \
        --profile-dir \"${REMOTE_BASE}/profiles\" \
        ${profiles_arg:+--profiles \"${profiles_arg}\"} \
        --output-dir \"${REMOTE_METRICS_DIR}\" \
        --shard-id \$i \
        --num-shards ${NUM_LOADERS} \
        > \"\$log_i\" 2>&1 < /dev/null &
      echo \$! >> \"${REMOTE_PID_FILE}\"
    done
    sync

    # Liveness check: traffic_gen.py can exit immediately on bad config
    # (no profiles selected, missing fields, parse errors, etc.). nohup
    # captures the PID before exit, so without this check the orchestrator
    # would happily wait through a multi-minute experiment with no traffic.
    # We check every shard — if ANY died, kill the survivors and bail.
    sleep 2
    dead=0
    while read -r p; do
      [[ -z \"\$p\" ]] && continue
      if ! kill -0 \"\$p\" 2>/dev/null; then
        echo \"ERROR: shard pid \$p exited within 2s of launch\" >&2
        dead=1
      fi
    done < \"${REMOTE_PID_FILE}\"
    if (( dead )); then
      for log in \"${REMOTE_BASE}\"/traffic_gen.shard*.log; do
        [[ -f \"\$log\" ]] || continue
        echo \"--- \$log (last 30 lines) ---\" >&2
        tail -n 30 \"\$log\" >&2 || true
      done
      while read -r p2; do
        [[ -n \"\$p2\" ]] && kill \"\$p2\" 2>/dev/null || true
      done < \"${REMOTE_PID_FILE}\"
      rm -f \"${REMOTE_PID_FILE}\"
      exit 1
    fi
    n=\$(wc -l < \"${REMOTE_PID_FILE}\" | tr -d \" \")
    echo \"started \$n shard(s): \$(tr \"\\n\" \" \" < ${REMOTE_PID_FILE})\"
  '"
}

stop_clients() {
  ${SSH} "bash -lc '
    set -euo pipefail
    if [[ ! -f \"${REMOTE_PID_FILE}\" ]]; then
      echo \"not running (no pid file)\"
      exit 0
    fi
    # SIGTERM each shard, give them 1s to flush, then SIGKILL anything that
    # is still alive. The shards close their CSV file handles in finally
    # blocks on SIGTERM, so a clean stop preserves all rows.
    while read -r pid; do
      [[ -z \"\$pid\" ]] && continue
      if kill -0 \"\$pid\" 2>/dev/null; then
        kill \"\$pid\" 2>/dev/null || true
      fi
    done < \"${REMOTE_PID_FILE}\"
    sleep 1
    while read -r pid; do
      [[ -z \"\$pid\" ]] && continue
      if kill -0 \"\$pid\" 2>/dev/null; then
        kill -9 \"\$pid\" 2>/dev/null || true
      fi
    done < \"${REMOTE_PID_FILE}\"
    # Safety net: kill any traffic_gen.py orphans not in the pid file
    # (e.g. if a PID was lost due to a truncated write).
    pkill -f \"traffic_gen.py\" 2>/dev/null || true
    n=\$(wc -l < \"${REMOTE_PID_FILE}\" | tr -d \" \")
    rm -f \"${REMOTE_PID_FILE}\"
    echo \"stopped \$n shard(s)\"
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
      shard=0
      while read -r pid; do
        [[ -z \"\$pid\" ]] && continue
        if kill -0 \"\$pid\" 2>/dev/null; then
          echo \"status=running shard=\$shard pid=\$pid\"
        else
          echo \"status=stale-pid shard=\$shard pid=\$pid\"
        fi
        shard=\$((shard+1))
      done < \"${REMOTE_PID_FILE}\"
    else
      echo \"status=stopped\"
    fi
    ls -lh \"${REMOTE_BASE}\"/traffic_gen*.log 2>/dev/null || true
    ls -lh \"${REMOTE_METRICS_DIR}\" 2>/dev/null || true
  '"
}

logs_clients() {
  ${SSH} "bash -lc '
    found=0
    for log in \"${REMOTE_BASE}\"/traffic_gen*.log; do
      [[ -f \"\$log\" ]] || continue
      found=1
      echo \"=== \$(basename \$log) ===\"
      tail -n 80 \"\$log\"
    done
    (( found )) || echo \"(no log files)\"
  '"
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
