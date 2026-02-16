#!/usr/bin/env bash
# ============================================================================
# Unified App Deployment — deploys any app from manifests/<app>/
# ============================================================================
# Runs LOCALLY and deploys to the remote K8s cluster over SSH.
# Requires: Istio + Gateway API installed (via deploy-istio.sh).
#
# Each app lives in its own manifests/<app>/ directory with:
#   app.conf   — required: APP_NAME, APP_NS, APP_GATEWAY_NAME, APP_HOST, APP_DEPLOYMENTS
#   test.sh    — optional: defines run_app_tests() for custom --test logic
#   *.yaml     — Kubernetes manifests (applied in lexicographic order)
#
# Usage:
#   ./deploy-app.sh <app>                # Deploy app
#   ./deploy-app.sh <app> --test         # Run traffic tests
#   ./deploy-app.sh <app> --status       # Show resource status
#   ./deploy-app.sh <app> --cleanup      # Remove all resources
#   ./deploy-app.sh <app> --help         # Show app-specific help
#   ./deploy-app.sh --list               # List available apps
#   ./deploy-app.sh --help               # Show this help
#
# Examples:
#   ./deploy-app.sh demo                 # Deploy canary routing demo
#   ./deploy-app.sh retry-budget --test  # Test retry budget demo
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/k8s-config.sh"

MANIFESTS_BASE="${SCRIPT_DIR}/manifests"

# ── Colours & helpers ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERR ]${NC}  $*" >&2; }
banner(){ echo -e "\n${GREEN}=== $* ===${NC}\n"; }

# ── SSH helpers ──────────────────────────────────────────────────────────────
ssh_cmd() {
    local host="$1"
    local cmd="ssh"
    [[ -n "${SSH_KEY}" ]] && cmd+=" -i ${SSH_KEY}"
    cmd+=" ${SSH_OPTS} ${SSH_USER}@${host}"
    echo "$cmd"
}

remote() {
    local host="$1"; shift
    local prefix
    prefix=$(eval "$(ssh_cmd "$host")" "hostname -s" 2>/dev/null || echo "$host")
    eval "$(ssh_cmd "$host")" "bash -s" <<-REMOTE_EOF 2>&1 | while IFS= read -r line; do echo -e "  ${CYAN}[${prefix}]${NC} ${line}"; done
$@
REMOTE_EOF
}

remote_capture() {
    local host="$1"; shift
    eval "$(ssh_cmd "$host")" "bash -s" <<-REMOTE_EOF 2>&1
$@
REMOTE_EOF
}

# ── App loading ──────────────────────────────────────────────────────────────
# These variables are set after load_app():
APP_NAME="" APP_NS="" APP_GATEWAY_NAME="" APP_HOST="" APP_DEPLOYMENTS=""
APP_DIR="" REMOTE_MANIFESTS_DIR=""

load_app() {
    local app="$1"
    APP_DIR="${MANIFESTS_BASE}/${app}"

    if [[ ! -d "${APP_DIR}" ]]; then
        err "App not found: ${app}"
        err "Directory does not exist: ${APP_DIR}"
        echo ""
        list_apps
        exit 1
    fi

    if [[ ! -f "${APP_DIR}/app.conf" ]]; then
        err "Missing app.conf in ${APP_DIR}/"
        err "Each app needs an app.conf with: APP_NAME, APP_NS, APP_GATEWAY_NAME, APP_HOST, APP_DEPLOYMENTS"
        exit 1
    fi

    # shellcheck source=/dev/null
    source "${APP_DIR}/app.conf"
    REMOTE_MANIFESTS_DIR="/tmp/${app}-manifests"

    # Validate required fields
    local missing=()
    [[ -z "$APP_NAME" ]]         && missing+=("APP_NAME")
    [[ -z "$APP_NS" ]]           && missing+=("APP_NS")
    [[ -z "$APP_GATEWAY_NAME" ]] && missing+=("APP_GATEWAY_NAME")
    [[ -z "$APP_HOST" ]]         && missing+=("APP_HOST")
    if (( ${#missing[@]} > 0 )); then
        err "app.conf is missing required fields: ${missing[*]}"
        exit 1
    fi
}

list_apps() {
    echo "Available apps (manifests/<app>/):"
    echo ""
    local found=0
    for conf in "${MANIFESTS_BASE}"/*/app.conf; do
        [[ -f "$conf" ]] || continue
        local dir name
        dir=$(dirname "$conf")
        # Source in a subshell to avoid polluting current scope
        name=$(APP_NAME=""; source "$conf" 2>/dev/null; echo "$APP_NAME")
        echo "  $(basename "$dir")  — ${name:-<no APP_NAME>}"
        ((found++)) || true
    done
    if (( found == 0 )); then
        echo "  (none found)"
    fi
    echo ""
}

# ── Common operations ────────────────────────────────────────────────────────
upload_manifests() {
    info "Uploading manifests to master (${MASTER_HOST})…"
    local scp_opts="${SSH_OPTS}"
    [[ -n "${SSH_KEY}" ]] && scp_opts+=" -i ${SSH_KEY}"

    eval "$(ssh_cmd "$MASTER_HOST")" "mkdir -p ${REMOTE_MANIFESTS_DIR}"
    # Upload yaml files and conf/sh files
    scp ${scp_opts} "${APP_DIR}"/*.yaml "${SSH_USER}@${MASTER_HOST}:${REMOTE_MANIFESTS_DIR}/"
    ok "Manifests uploaded to ${REMOTE_MANIFESTS_DIR}/"
}

get_node_port() {
    local result
    result=$(remote_capture "$MASTER_HOST" "
        kubectl get svc -n ${APP_NS} \
            -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
            -o jsonpath='{.items[0].spec.ports[?(@.name==\"http\")].nodePort}' 2>/dev/null || true
    " 2>/dev/null || true)
    echo "$result" | tr -d '[:space:]'
}

# ============================================================================
# Deploy
# ============================================================================
do_deploy() {
    banner "Deploying: ${APP_NAME}"
    info "Manifests: ${APP_DIR}/"
    echo ""

    upload_manifests

    # Build the list of rollout-status waits
    local wait_cmds=""
    for dep in ${APP_DEPLOYMENTS}; do
        wait_cmds+="kubectl -n ${APP_NS} rollout status deployment/${dep} --timeout=120s"$'\n'
    done

    remote "$MASTER_HOST" "
        M='${REMOTE_MANIFESTS_DIR}'

        # Apply namespace first (so other resources have a target namespace)
        if [[ -f \${M}/namespace.yaml ]]; then
            echo '=== Applying namespace.yaml ==='
            kubectl apply -f \${M}/namespace.yaml
            echo ''
        fi

        # Apply all manifests (idempotent for namespace.yaml)
        echo '=== Applying all manifests ==='
        kubectl apply -f \${M}/
        echo ''

        # Wait for deployments
        echo '=== Waiting for pods ==='
        ${wait_cmds}

        echo ''
        echo 'Waiting for gateway proxy pod…'
        sleep 15

        # Re-apply XBackendTrafficPolicy after Services are up
        # (Istio may report TargetNotFound if BTP is applied before the Service exists)
        for f in \${M}/*traffic-policy*.yaml; do
            [[ -f \"\$f\" ]] && kubectl apply -f \"\$f\" 2>/dev/null || true
        done

        echo ''
        echo '=== Resource summary ==='
        kubectl -n ${APP_NS} get po,svc,gateway,httproute 2>/dev/null || true
        echo ''
        kubectl -n ${APP_NS} get destinationrule 2>/dev/null || true
        kubectl -n ${APP_NS} get xbackendtrafficpolicies.gateway.networking.x-k8s.io 2>/dev/null || true
    "

    local node_port
    node_port=$(get_node_port)

    ok "${APP_NAME} deployed."
    echo ""
    if [[ -n "$node_port" ]]; then
        info "Gateway NodePort: ${node_port}"
        echo ""
        echo "  Test:   ./deploy-app.sh $(basename "${APP_DIR}") --test"
        echo "  Manual: curl -s -H 'Host: ${APP_HOST}' http://${MASTER_HOST}:${node_port}/"
    else
        warn "Could not detect Gateway NodePort yet."
        echo "  Run: ./deploy-app.sh $(basename "${APP_DIR}") --status"
    fi
}

# ============================================================================
# Test
# ============================================================================
do_test() {
    banner "Traffic Test: ${APP_NAME}"

    local node_port
    node_port=$(get_node_port)

    if [[ -z "$node_port" ]]; then
        err "Gateway NodePort not found. Is the app deployed?"
        err "Run:  ./deploy-app.sh $(basename "${APP_DIR}")"
        exit 1
    fi

    local gateway_url="http://${MASTER_HOST}:${node_port}"
    info "Gateway URL: ${gateway_url}"
    echo ""

    # Source custom test logic if available
    if [[ -f "${APP_DIR}/test.sh" ]]; then
        # shellcheck source=/dev/null
        source "${APP_DIR}/test.sh"
        run_app_tests "$gateway_url"
    else
        # Default: send 10 requests and show responses
        echo -e "${CYAN}--- Sending 10 requests ---${NC}"
        echo ""
        local response
        for i in $(seq 1 10); do
            response=$(curl -s --connect-timeout 5 -H "Host: ${APP_HOST}" "${gateway_url}/" 2>/dev/null) || response="(request failed)"
            echo "  [$i] $response"
        done
    fi

    echo ""
    ok "Tests complete."
    echo ""
    echo "  Manual testing:"
    echo "    curl -s -H 'Host: ${APP_HOST}' ${gateway_url}/"
    echo "    curl -sv -H 'Host: ${APP_HOST}' ${gateway_url}/  # verbose"
}

# ============================================================================
# Status
# ============================================================================
do_status() {
    banner "Status: ${APP_NAME}"
    remote "$MASTER_HOST" "
        if ! kubectl get namespace ${APP_NS} &>/dev/null; then
            echo 'Namespace ${APP_NS} not found. App is not deployed.'
            echo 'Run:  ./deploy-app.sh $(basename "${APP_DIR}")'
            exit 0
        fi

        echo '--- Pods ---'
        kubectl -n ${APP_NS} get pods -o wide
        echo ''

        echo '--- Services ---'
        kubectl -n ${APP_NS} get svc
        echo ''

        echo '--- Gateway ---'
        kubectl -n ${APP_NS} get gateway
        echo ''

        echo '--- HTTPRoutes ---'
        kubectl -n ${APP_NS} get httproute
        echo ''

        echo '--- DestinationRules ---'
        kubectl -n ${APP_NS} get destinationrule 2>/dev/null || true
        echo ''

        echo '--- BackendTrafficPolicy ---'
        kubectl -n ${APP_NS} get xbackendtrafficpolicies.gateway.networking.x-k8s.io 2>/dev/null || true
        echo ''

        echo '--- Istio proxies ---'
        istioctl proxy-status 2>/dev/null | grep ${APP_NS} || echo '(none)'
    "

    local node_port
    node_port=$(get_node_port)
    if [[ -n "$node_port" ]]; then
        echo ""
        info "Gateway NodePort: ${node_port}"
        echo "  curl -s -H 'Host: ${APP_HOST}' http://${MASTER_HOST}:${node_port}/"
    fi
}

# ============================================================================
# Cleanup
# ============================================================================
do_cleanup() {
    banner "Removing: ${APP_NAME}"

    upload_manifests 2>/dev/null || true

    remote "$MASTER_HOST" "
        M='${REMOTE_MANIFESTS_DIR}'

        echo 'Removing all resources…'
        kubectl delete -f \${M}/ 2>/dev/null || true

        rm -rf \${M}

        echo ''
        echo 'Cleanup complete.'
    "
    ok "${APP_NAME} removed."
}

# ============================================================================
# App help
# ============================================================================
do_app_help() {
    local app_slug
    app_slug=$(basename "${APP_DIR}")

    echo "App:  ${APP_NAME}"
    echo "Dir:  manifests/${app_slug}/"
    echo "NS:   ${APP_NS}"
    echo ""
    echo "Usage: $0 ${app_slug} [OPTION]"
    echo ""
    echo "Options:"
    echo "  (none)      Deploy all manifests"
    echo "  --test      Run traffic tests from local machine"
    echo "  --status    Show resource status"
    echo "  --cleanup   Remove all resources"
    echo "  --help      Show this help"
    echo ""
    echo "Manifests:"
    for f in "${APP_DIR}"/*.yaml; do
        [[ -f "$f" ]] && echo "  $(basename "$f")"
    done
    echo ""
    [[ -f "${APP_DIR}/test.sh" ]] && echo "Custom test: test.sh" || echo "Test: default (10 requests)"
}

# ============================================================================
# CLI
# ============================================================================
usage() {
    echo "Usage: $0 <app> [OPTION]"
    echo ""
    echo "Unified deployment script for apps in manifests/<app>/."
    echo "Requires: deploy-k8s.sh + deploy-istio.sh already run."
    echo ""
    echo "Arguments:"
    echo "  <app>         Name of the app directory under manifests/"
    echo ""
    echo "Options:"
    echo "  (none)        Deploy all manifests for the app"
    echo "  --test        Run traffic tests from local machine"
    echo "  --status      Show resource status"
    echo "  --cleanup     Remove all resources"
    echo "  --help        Show app-specific help"
    echo ""
    echo "Global options:"
    echo "  --list        List available apps"
    echo "  --help        Show this help (when no <app> given)"
    echo ""
    list_apps
}

main() {
    # Global flags (no app required)
    case "${1:-}" in
        --list)
            list_apps
            exit 0
            ;;
        --help|-h|"")
            usage
            exit 0
            ;;
    esac

    # First arg is the app name
    local app="$1"
    local action="${2:-}"

    load_app "$app"

    case "$action" in
        "")         do_deploy ;;
        --test)     do_test ;;
        --status)   do_status ;;
        --cleanup)  do_cleanup ;;
        --help|-h)  do_app_help ;;
        *)          err "Unknown option: $action (try --help)"; exit 1 ;;
    esac
}

main "$@"
