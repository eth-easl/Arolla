#!/usr/bin/env bash
# ============================================================================
# Demo App Deployment — Gateway API Ingress with Canary Routing
# ============================================================================
# Runs LOCALLY and deploys to the remote K8s cluster over SSH.
# Requires: Istio + Gateway API installed (via deploy-istio.sh).
#
# Applies YAML manifests from manifests/demo/:
#   namespace.yaml          — istio-test namespace (sidecar injection)
#   backend-v1.yaml         — Deployment + Service (3 replicas, http-echo)
#   backend-v2.yaml         — Deployment + Service (2 replicas, http-echo)
#   gateway.yaml            — Gateway (HTTP/80, istio class, NodePort)
#   httproute.yaml          — HTTPRoute with 50/50 canary split
#   destination-rules.yaml  — DestinationRules (circuit breaking, etc.)
#   client.yaml             — Client config (ConfigMap: hostname, endpoints, split)
#
# Edit those YAML files directly to change the configuration.
#
# Reference: https://devopscube.com/istio-ingress-kubernetes-gateway-api/
#
# Usage:
#   ./deploy-demo.sh                # Deploy all manifests
#   ./deploy-demo.sh --test         # Run traffic tests from local machine
#   ./deploy-demo.sh --status       # Show demo resource status
#   ./deploy-demo.sh --cleanup      # Remove all demo resources
#   ./deploy-demo.sh --help         # Show help
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/k8s-config.sh"

MANIFESTS_DIR="${SCRIPT_DIR}/manifests/demo"
REMOTE_MANIFESTS_DIR="/tmp/istio-demo-manifests"

# ── Colours & helpers ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERR ]${NC}  $*" >&2; }
banner(){ echo -e "\n${GREEN}=== $* ===${NC}\n"; }

# Build SSH command prefix for a host
ssh_cmd() {
    local host="$1"
    local cmd="ssh"
    [[ -n "${SSH_KEY}" ]] && cmd+=" -i ${SSH_KEY}"
    cmd+=" ${SSH_OPTS} ${SSH_USER}@${host}"
    echo "$cmd"
}

# Run a command on a remote host, streaming output
remote() {
    local host="$1"; shift
    local prefix
    prefix=$(eval "$(ssh_cmd "$host")" "hostname -s" 2>/dev/null || echo "$host")
    eval "$(ssh_cmd "$host")" "bash -s" <<-REMOTE_EOF 2>&1 | while IFS= read -r line; do echo -e "  ${CYAN}[${prefix}]${NC} ${line}"; done
$@
REMOTE_EOF
}

# Run a command on remote and capture output (no prefix)
remote_capture() {
    local host="$1"; shift
    eval "$(ssh_cmd "$host")" "bash -s" <<-REMOTE_EOF 2>&1
$@
REMOTE_EOF
}

# Upload manifests to master node via scp
upload_manifests() {
    info "Uploading manifests to master (${MASTER_HOST})…"
    local scp_opts="${SSH_OPTS}"
    [[ -n "${SSH_KEY}" ]] && scp_opts+=" -i ${SSH_KEY}"

    eval "$(ssh_cmd "$MASTER_HOST")" "mkdir -p ${REMOTE_MANIFESTS_DIR}"
    scp ${scp_opts} "${MANIFESTS_DIR}"/*.yaml "${SSH_USER}@${MASTER_HOST}:${REMOTE_MANIFESTS_DIR}/"
    ok "Manifests uploaded to ${REMOTE_MANIFESTS_DIR}/"
}

# Get the Gateway NodePort by querying the master.
# Returns the port number on stdout, or empty string if not found.
get_node_port() {
    local result
    result=$(remote_capture "$MASTER_HOST" '
        kubectl get svc -n istio-test -l gateway.networking.k8s.io/gateway-name=istio-gateway \
            -o jsonpath="{.items[0].spec.ports[?(@.name==\"http\")].nodePort}" 2>/dev/null || true
    ' 2>/dev/null || true)
    echo "$result" | tr -d '[:space:]'
}

# ============================================================================
# Deploy
# ============================================================================
deploy() {
    banner "Deploying demo: Gateway API ingress with canary routing"
    info "Manifests: ${MANIFESTS_DIR}/"
    echo ""

    upload_manifests

    remote "$MASTER_HOST" "
        M='${REMOTE_MANIFESTS_DIR}'

        # 1. Namespace
        echo '=== Applying namespace.yaml ==='
        kubectl apply -f \${M}/namespace.yaml
        echo ''

        # 2. Applications
        echo '=== Applying backend-v1.yaml ==='
        kubectl apply -f \${M}/backend-v1.yaml

        echo '=== Applying backend-v2.yaml ==='
        kubectl apply -f \${M}/backend-v2.yaml
        echo ''

        # 3. Gateway
        echo '=== Applying gateway.yaml ==='
        kubectl apply -f \${M}/gateway.yaml
        echo ''

        # 4. HTTPRoute
        echo '=== Applying httproute.yaml ==='
        kubectl apply -f \${M}/httproute.yaml
        echo ''

        # 5. DestinationRules
        echo '=== Applying destination-rules.yaml ==='
        kubectl apply -f \${M}/destination-rules.yaml
        echo ''

        # 6. Client config
        echo '=== Applying client.yaml ==='
        kubectl apply -f \${M}/client.yaml
        echo ''

        # Wait for readiness
        echo '=== Waiting for pods to be ready ==='
        kubectl -n istio-test rollout status deployment/backend-v1 --timeout=120s
        kubectl -n istio-test rollout status deployment/backend-v2 --timeout=120s

        echo ''
        echo 'Waiting for gateway proxy pod…'
        sleep 15

        echo ''
        echo '=== Resource summary ==='
        kubectl -n istio-test get po,svc,gateway,httproute,destinationrule
    "

    # Fetch NodePort and print test instructions
    local node_port
    node_port=$(get_node_port)

    ok "Demo deployed."
    echo ""
    if [[ -n "$node_port" ]]; then
        info "Gateway NodePort: ${node_port}"
        echo ""
        echo "  Test from your local machine:"
        echo "    curl -s -H 'Host: test.example.com' http://${MASTER_HOST}:${node_port}/"
        echo ""
        echo "  Or run:  ./deploy-demo.sh --test"
    else
        warn "Could not detect Gateway NodePort yet. Run: ./deploy-demo.sh --status"
    fi
}

# ============================================================================
# Test — runs curl from local machine through the Gateway NodePort
# ============================================================================
run_tests() {
    banner "Traffic Validation (from local machine)"

    local node_port
    node_port=$(get_node_port)

    if [[ -z "$node_port" ]]; then
        err "Gateway NodePort not found. Is the demo deployed?"
        err "Run:  ./deploy-demo.sh"
        exit 1
    fi

    local gateway_url="http://${MASTER_HOST}:${node_port}"
    info "Gateway URL: ${gateway_url}"
    echo ""

    # ------------------------------------------------------------------
    # North-south: curl through the Gateway from local machine
    # ------------------------------------------------------------------
    echo -e "${CYAN}--- Canary traffic split (10 requests) ---${NC}"
    echo ""
    local response
    for i in $(seq 1 10); do
        response=$(curl -s --connect-timeout 5 -H 'Host: test.example.com' "${gateway_url}/" 2>/dev/null) || response="(request failed)"
        echo "  [$i] $response"
    done
    echo ""
    echo -e "${CYAN}Expected: roughly 50/50 split between v1 and v2.${NC}"

    echo ""
    echo -e "${CYAN}--- Single request with verbose headers ---${NC}"
    echo ""
    curl -sv --connect-timeout 5 -H 'Host: test.example.com' "${gateway_url}/" 2>&1 || true

    echo ""
    ok "Tests complete."
    echo ""
    echo "  Manual testing:"
    echo "    curl -s -H 'Host: test.example.com' ${gateway_url}/"
    echo "    curl -sv -H 'Host: test.example.com' ${gateway_url}/  # verbose, see headers"
}

# ============================================================================
# Status
# ============================================================================
show_status() {
    banner "Demo App Status"
    remote "$MASTER_HOST" "
        if ! kubectl get namespace istio-test &>/dev/null; then
            echo 'Namespace istio-test not found. Demo is not deployed.'
            echo 'Run:  ./deploy-demo.sh'
            exit 0
        fi

        echo '--- Pods ---'
        kubectl -n istio-test get pods -o wide
        echo ''

        echo '--- Services ---'
        kubectl -n istio-test get svc
        echo ''

        echo '--- Gateway ---'
        kubectl -n istio-test get gateway
        echo ''

        echo '--- HTTPRoutes ---'
        kubectl -n istio-test get httproute
        echo ''

        echo '--- DestinationRules ---'
        kubectl -n istio-test get destinationrule
        echo ''

        echo '--- Istio proxies in this namespace ---'
        istioctl proxy-status 2>/dev/null | grep istio-test || echo '(none)'
    "

    # Show NodePort for convenience
    local node_port
    node_port=$(get_node_port)
    if [[ -n "$node_port" ]]; then
        echo ""
        info "Gateway NodePort: ${node_port}"
        echo "  curl -s -H 'Host: test.example.com' http://${MASTER_HOST}:${node_port}/"
    fi
}

# ============================================================================
# Cleanup
# ============================================================================
cleanup() {
    banner "Removing demo app"

    upload_manifests 2>/dev/null || true

    remote "$MASTER_HOST" "
        M='${REMOTE_MANIFESTS_DIR}'

        echo 'Removing resources in reverse order…'
        echo ''

        kubectl delete -f \${M}/client.yaml 2>/dev/null || true
        kubectl delete -f \${M}/destination-rules.yaml 2>/dev/null || true
        kubectl delete -f \${M}/httproute.yaml 2>/dev/null || true
        kubectl delete -f \${M}/gateway.yaml 2>/dev/null || true
        kubectl delete -f \${M}/backend-v2.yaml 2>/dev/null || true
        kubectl delete -f \${M}/backend-v1.yaml 2>/dev/null || true
        kubectl delete -f \${M}/namespace.yaml 2>/dev/null || true

        rm -rf \${M}

        echo ''
        echo 'Demo cleanup complete.'
    "
    ok "Demo app removed"
}

# ============================================================================
# CLI
# ============================================================================
main() {
    if [[ ! -d "${MANIFESTS_DIR}" ]]; then
        err "Manifests directory not found: ${MANIFESTS_DIR}"
        err "Expected YAML files in manifests/demo/"
        exit 1
    fi

    case "${1:-}" in
        --test)
            run_tests
            ;;
        --status)
            show_status
            ;;
        --cleanup)
            cleanup
            ;;
        --help|-h)
            echo "Usage: $0 [OPTION]"
            echo ""
            echo "Deploys the demo app on the Istio + Gateway API platform."
            echo "Requires: deploy-k8s.sh + deploy-istio.sh already run."
            echo ""
            echo "Options:"
            echo "  (none)      Deploy all manifests from manifests/demo/"
            echo "  --test      Run traffic tests from local machine via Gateway NodePort"
            echo "  --status    Show demo resource status"
            echo "  --cleanup   Remove all demo resources"
            echo "  --help      Show this help"
            echo ""
            echo "Manifests (manifests/demo/):"
            echo "  namespace.yaml          istio-test namespace (sidecar injection)"
            echo "  backend-v1.yaml         Deployment + Service (3 replicas)"
            echo "  backend-v2.yaml         Deployment + Service (2 replicas)"
            echo "  gateway.yaml            Gateway (HTTP/80, NodePort, istio class)"
            echo "  httproute.yaml          HTTPRoute (50/50 canary split)"
            echo "  destination-rules.yaml  DestinationRules (circuit breaking)"
            echo "  client.yaml             Client config (hostname, endpoints, split)"
            echo ""
            echo "Edit the YAML files directly to change configuration."
            echo "Reference: https://devopscube.com/istio-ingress-kubernetes-gateway-api/"
            ;;
        "")
            deploy
            ;;
        *)
            err "Unknown option: $1 (try --help)"
            exit 1
            ;;
    esac
}

main "$@"
