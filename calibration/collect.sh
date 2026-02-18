#!/usr/bin/env bash
# =============================================================================
# collect.sh — Collect Envoy sidecar metrics from running online-boutique pods.
#
# Runs LOCALLY, reaches the cluster via SSH + kubectl exec.
# Requires: online-boutique already deployed (deploy-app.sh online-boutique).
#
# Steps:
#   1. Find the first Running pod for each service
#   2. Reset all Envoy counters (POST /reset_counters)
#   3. Wait WAIT_SECS for traffic to accumulate
#   4. Dump each pod's /stats to raw_stats/<service>.txt
#
# Usage:
#   ./collect.sh [--wait 60] [--ns online-boutique] [--output raw_stats/]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../prototype/k8s-config.sh"

# ── Defaults ─────────────────────────────────────────────────────────────────
WAIT_SECS=60
NAMESPACE="online-boutique"
OUTPUT_DIR="${SCRIPT_DIR}/data/raw_stats"

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait)   WAIT_SECS="$2";   shift 2 ;;
        --ns)     NAMESPACE="$2";   shift 2 ;;
        --output) OUTPUT_DIR="$2";  shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ── Colours & helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }

# ── SSH helpers (mirrors deploy-app.sh exactly) ───────────────────────────────
ssh_cmd() {
    local host="$1"
    local cmd="ssh"
    [[ -n "${SSH_KEY}" ]] && cmd+=" -i ${SSH_KEY}"
    cmd+=" ${SSH_OPTS} ${SSH_USER}@${host}"
    echo "$cmd"
}

remote_capture() {
    local host="$1"; shift
    eval "$(ssh_cmd "$host")" "bash -s" <<-REMOTE_EOF 2>&1
$@
REMOTE_EOF
}

# ── Service list (parallel arrays — bash 3.2 compatible) ─────────────────────
# Services in the same order as topology.py (order doesn't matter here).
SERVICES=(
    frontend
    cartservice
    checkoutservice
    currencyservice
    emailservice
    paymentservice
    productcatalogservice
    recommendationservice
    shippingservice
    adservice
    redis-cart
)
PORTS=(
    8080    # frontend
    7070    # cartservice
    5050    # checkoutservice
    7000    # currencyservice
    8080    # emailservice
    50051   # paymentservice
    3550    # productcatalogservice
    8080    # recommendationservice
    50051   # shippingservice
    9555    # adservice
    6379    # redis-cart (TCP)
)

# ── Main ──────────────────────────────────────────────────────────────────────
mkdir -p "${OUTPUT_DIR}"

info "Namespace: ${NAMESPACE}"
info "Output:    ${OUTPUT_DIR}/"
info "Window:    ${WAIT_SECS}s"
echo ""

# Step 1: Discover pod names for all services (single SSH call for efficiency)
info "Discovering pods on ${MASTER_HOST}..."

SVC_LIST="${SERVICES[*]}"
ALL_PODS=$(remote_capture "$MASTER_HOST" "
    for SVC in ${SVC_LIST}; do
        POD=\$(kubectl -n ${NAMESPACE} get pod -l app=\${SVC} \
            --field-selector=status.phase=Running \
            --sort-by=metadata.name \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo '')
        POD=\$(echo \"\${POD}\" | tr -d \"' \n\")
        echo \"\${SVC}=\${POD}\"
    done
") || true

# Parse pod names into an array indexed same as SERVICES/PORTS
declare -a POD_NAMES
for i in "${!SERVICES[@]}"; do
    SVC="${SERVICES[$i]}"
    POD=$(printf '%s\n' "${ALL_PODS}" | grep "^${SVC}=" | cut -d= -f2 | tr -d "' \n" || true)
    POD_NAMES[$i]="${POD:-}"
    if [[ -n "${POD_NAMES[$i]}" ]]; then
        ok "  ${SVC}: ${POD_NAMES[$i]}"
    else
        warn "  ${SVC}: no Running pod found — will skip"
    fi
done
echo ""

# Step 2: Reset Envoy counters on all discovered pods
info "Resetting Envoy counters (histograms are NOT reset; only request counters)..."
for i in "${!SERVICES[@]}"; do
    POD="${POD_NAMES[$i]}"
    [[ -z "${POD}" ]] && continue
    SVC="${SERVICES[$i]}"
    remote_capture "$MASTER_HOST" "
        kubectl exec -n ${NAMESPACE} ${POD} -c istio-proxy -- \
            curl -s -X POST localhost:15000/reset_counters > /dev/null 2>&1 || true
    " > /dev/null
    info "  Reset ${SVC}"
done
ok "Counters reset."
echo ""

# Step 3: Wait for traffic to accumulate
info "Waiting ${WAIT_SECS}s for measurement window..."
sleep "${WAIT_SECS}"
COLLECTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
ok "Window complete."
echo ""

# Step 4: Collect /stats from each pod
info "Collecting Envoy stats..."
COLLECTED=0
SKIPPED=0

for i in "${!SERVICES[@]}"; do
    SVC="${SERVICES[$i]}"
    PORT="${PORTS[$i]}"
    POD="${POD_NAMES[$i]}"

    if [[ -z "${POD}" ]]; then
        ((SKIPPED++)) || true
        continue
    fi

    OUTFILE="${OUTPUT_DIR}/${SVC}.txt"

    # Write metadata header (parsed by fit.py)
    echo "# SERVICE=${SVC}  PORT=${PORT}  POD=${POD}  ELAPSED_SECS=${WAIT_SECS}  COLLECTED_AT=${COLLECTED_AT}" > "${OUTFILE}"

    # Append raw Envoy stats dump
    remote_capture "$MASTER_HOST" "
        kubectl exec -n ${NAMESPACE} ${POD} -c istio-proxy -- \
            curl -s localhost:15000/stats 2>/dev/null || true
    " >> "${OUTFILE}"

    local_lines=$(wc -l < "${OUTFILE}" | tr -d ' ')
    ok "  ${SVC}.txt  (${local_lines} lines)"
    ((COLLECTED++)) || true
done

echo ""
ok "Collected: ${COLLECTED}  Skipped: ${SKIPPED}"
info "Stats in: ${OUTPUT_DIR}/"
