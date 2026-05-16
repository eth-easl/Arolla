#!/usr/bin/env bash
# ============================================================================
# Istio + Gateway API — Platform Install
# ============================================================================
# Runs LOCALLY and configures the remote K8s cluster over SSH.
# Requires: a running Kubernetes cluster (deployed via deploy-k8s.sh).
#
# This script installs the service mesh platform layer:
#   1. istioctl CLI
#   2. Kubernetes Gateway API CRDs
#   3. Istio control plane (istiod)
#   4. Verify GatewayClass + health
#
# After this, deploy applications with: ./deploy-app.sh <app>
#
# Reference: https://devopscube.com/istio-ingress-kubernetes-gateway-api/
#
# Usage:
#   ./deploy-istio.sh              # Full install (all steps)
#   ./deploy-istio.sh --step N     # Run only step N (1-4)
#   ./deploy-istio.sh --status     # Show Istio & Gateway API status
#   ./deploy-istio.sh --cleanup    # Remove Istio + Gateway API CRDs
#   ./deploy-istio.sh --help       # Show help
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/k8s-config.sh"

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

# ============================================================================
# Install Steps (1-4)
# ============================================================================

# ── Step 1: Install istioctl on master ───────────────────────────────────────
step1_install_istioctl() {
    banner "Step 1: Install istioctl ${ISTIO_VERSION} on master"
    remote "$MASTER_HOST" "
        ISTIO_VERSION='${ISTIO_VERSION}'

        # Check if istioctl is already installed with correct version
        if command -v istioctl &>/dev/null; then
            CURRENT=\$(istioctl version --remote=false 2>/dev/null || echo 'unknown')
            if [[ \"\$CURRENT\" == \"\$ISTIO_VERSION\" ]]; then
                echo \"istioctl \${ISTIO_VERSION} already installed, skipping.\"
                istioctl version --remote=false
                exit 0
            fi
            echo \"Upgrading istioctl from \$CURRENT to \$ISTIO_VERSION…\"
        fi

        echo \"Downloading Istio \${ISTIO_VERSION}…\"
        curl -sL https://istio.io/downloadIstio | ISTIO_VERSION=\$ISTIO_VERSION sh -

        sudo cp istio-\${ISTIO_VERSION}/bin/istioctl /usr/local/bin/istioctl
        sudo chmod +x /usr/local/bin/istioctl

        echo 'Cleaning up tarball…'
        rm -rf istio-\${ISTIO_VERSION}

        echo 'Installed:'
        istioctl version --remote=false
    "
    ok "istioctl installed on master"

    # Also install istioctl locally so you can run istioctl commands from your machine.
    # Install to ~/.local/bin/ to avoid requiring sudo on macOS/Linux.
    banner "Step 1b: Install istioctl ${ISTIO_VERSION} locally"
    LOCAL_BIN="${HOME}/.local/bin"
    mkdir -p "${LOCAL_BIN}"
    if command -v istioctl &>/dev/null; then
        CURRENT=$(istioctl version --remote=false 2>/dev/null || echo 'unknown')
        if [[ "$CURRENT" == "${ISTIO_VERSION}" ]]; then
            ok "istioctl ${ISTIO_VERSION} already installed locally, skipping."
        else
            info "Upgrading local istioctl from $CURRENT to ${ISTIO_VERSION}…"
            curl -sL https://istio.io/downloadIstio | ISTIO_VERSION=${ISTIO_VERSION} sh -
            cp "istio-${ISTIO_VERSION}/bin/istioctl" "${LOCAL_BIN}/istioctl"
            chmod +x "${LOCAL_BIN}/istioctl"
            rm -rf "istio-${ISTIO_VERSION}"
            ok "istioctl ${ISTIO_VERSION} installed to ${LOCAL_BIN}/istioctl"
        fi
    else
        info "Downloading istioctl ${ISTIO_VERSION} for local use…"
        curl -sL https://istio.io/downloadIstio | ISTIO_VERSION=${ISTIO_VERSION} sh -
        cp "istio-${ISTIO_VERSION}/bin/istioctl" "${LOCAL_BIN}/istioctl"
        chmod +x "${LOCAL_BIN}/istioctl"
        rm -rf "istio-${ISTIO_VERSION}"
        ok "istioctl ${ISTIO_VERSION} installed to ${LOCAL_BIN}/istioctl"
        info "Make sure ${LOCAL_BIN} is in your PATH: export PATH=\"\$PATH:${LOCAL_BIN}\""
    fi
}

# ── Step 2: Install Gateway API CRDs ────────────────────────────────────────
step2_install_gateway_api_crds() {
    banner "Step 2: Install Kubernetes Gateway API CRDs ${GATEWAY_API_VERSION}"
    remote "$MASTER_HOST" "
        GATEWAY_API_VERSION='${GATEWAY_API_VERSION}'

        echo 'Checking for existing Gateway API CRDs…'
        if kubectl get crd gateways.gateway.networking.k8s.io &>/dev/null; then
            echo 'Gateway API CRDs already present. Updating…'
        fi

        echo \"Installing Gateway API CRDs \${GATEWAY_API_VERSION} (experimental channel)…\"
        echo '(Experimental channel includes BackendTrafficPolicy for retry budgets)'
        kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/\${GATEWAY_API_VERSION}/experimental-install.yaml

        echo ''
        echo 'Installed Gateway API CRDs:'
        kubectl get crd | grep gateway.networking.k8s.io || echo '(none found)'
    "
    ok "Gateway API CRDs installed"
}

# ── Step 3: Install Istio control plane ──────────────────────────────────────
step3_install_istio() {
    banner "Step 3: Install Istio control plane (profile: ${ISTIO_PROFILE})"
    remote "$MASTER_HOST" "
        ISTIO_PROFILE='${ISTIO_PROFILE}'
        ISTIO_NAMESPACE='${ISTIO_NAMESPACE}'

        echo 'Running pre-flight checks…'
        istioctl x precheck

        echo ''
        echo \"Installing Istio with profile: \${ISTIO_PROFILE}…\"
        # Mesh-wide sidecar CPU reservation bumped from Istios default
        # 10 m to 100 m. The bump is the production recommendation for
        # any deployment that wants a reservable observation channel
        # through Envoy (which this paper does -- the RL controller
        # polls /stats every 2 s). No limits block is set, so a sidecar
        # can still burst above 100 m if its node has idle CPU; the
        # 100 m is a floor under contention, not a quota. See
        # manifests/istio/sidecar-cpu-reservation.yaml for the YAML
        # form + sizing rationale.
        istioctl install --set profile=\${ISTIO_PROFILE} \
            --set values.pilot.resources.requests.memory=512Mi \
            --set values.pilot.resources.requests.cpu=250m \
            --set values.pilot.env.PILOT_ENABLE_ALPHA_GATEWAY_API=true \
            --set values.global.proxy.resources.requests.cpu=100m \
            --set values.global.proxy.resources.requests.memory=128Mi \
            --set meshConfig.accessLogFile=/dev/stdout \
            --set 'meshConfig.defaultConfig.proxyStatsMatcher.inclusionRegexps[0]=.*upstream_rq_retry.*' \
            --set 'meshConfig.defaultConfig.proxyStatsMatcher.inclusionRegexps[1]=.*upstream_rq_completed' \
            --set 'meshConfig.defaultConfig.proxyStatsMatcher.inclusionRegexps[2]=.*upstream_rq_total' \
            --set 'meshConfig.defaultConfig.proxyStatsMatcher.inclusionRegexps[3]=.*upstream_rq_[0-9]+' \
            -y

        echo ''
        echo 'Waiting for Istio pods to be ready…'
        kubectl -n \${ISTIO_NAMESPACE} rollout status deployment/istiod --timeout=120s

        echo ''
        echo 'Istio control plane pods:'
        kubectl get pods -n \${ISTIO_NAMESPACE}

        echo ''
        istioctl version
    "
    ok "Istio control plane installed"

    # Also push the IstioOperator YAML form to the cluster so the bump
    # is documented in-tree. The --set flags above already set the
    # value; this kubectl-apply is here so any later operator who
    # inspects the cluster sees the manifest annotation rather than
    # having to reconstruct the bump from the install command line.
    banner "Step 3b: Apply sidecar-cpu-reservation manifest"
    local sidecar_manifest="${SCRIPT_DIR}/manifests/istio/sidecar-cpu-reservation.yaml"
    if [[ ! -f "${sidecar_manifest}" ]]; then
        warn "sidecar-cpu-reservation.yaml not found at ${sidecar_manifest}; skipping apply"
        return 0
    fi
    local scp_opts="${SSH_OPTS}"
    [[ -n "${SSH_KEY}" ]] && scp_opts+=" -i ${SSH_KEY}"
    # shellcheck disable=SC2086
    scp ${scp_opts} "${sidecar_manifest}" \
        "${SSH_USER}@${MASTER_HOST}:/tmp/sidecar-cpu-reservation.yaml" >/dev/null
    remote "$MASTER_HOST" "
        echo 'Applying IstioOperator sidecar-cpu-reservation patch…'
        # istioctl install merges into the existing operator state. The
        # --set flags from Step 3 already wrote the same values; this
        # second apply is the documented in-tree source of truth and is
        # a no-op on the cluster state if Step 3 already installed.
        istioctl install -f /tmp/sidecar-cpu-reservation.yaml -y
        echo ''
        echo 'Verifying mesh-wide sidecar CPU request reservation…'
        kubectl -n ${ISTIO_NAMESPACE} get IstioOperator installed-state \
          -o jsonpath='{.spec.values.global.proxy.resources.requests.cpu}'
        echo ''
    "
    ok "Sidecar CPU reservation (mesh-wide 100 m) applied"
}

# ── Step 4: Verify GatewayClass + installation ──────────────────────────────
step4_verify() {
    banner "Step 4: Verify Istio + Gateway API installation"
    remote "$MASTER_HOST" "
        echo '--- Istio version ---'
        istioctl version
        echo ''

        echo '--- Istio GatewayClasses (auto-created by Istio) ---'
        echo 'Istio creates two GatewayClasses:'
        echo '  istio        — built-in controller that manages Gateway resources'
        echo '  istio-remote — for gateways managed by a remote cluster'
        kubectl get gatewayclass
        echo ''

        echo '--- Istio system pods ---'
        kubectl get pods -n ${ISTIO_NAMESPACE}
        echo ''

        echo '--- Gateway API CRDs ---'
        kubectl get crd | grep gateway.networking.k8s.io
        echo ''

        echo '--- Istio analyze (check for issues) ---'
        istioctl analyze -A 2>/dev/null || true
    "
    ok "Verification complete"
}

# ============================================================================
# Status
# ============================================================================
show_status() {
    banner "Istio + Gateway API Status"
    remote "$MASTER_HOST" "
        echo '--- Istio version ---'
        istioctl version 2>/dev/null || echo 'istioctl not found'
        echo ''

        echo '--- GatewayClasses ---'
        kubectl get gatewayclass 2>/dev/null || echo '(none)'
        echo ''

        echo '--- Istio system pods ---'
        kubectl get pods -n ${ISTIO_NAMESPACE} 2>/dev/null || echo '(namespace not found)'
        echo ''

        echo '--- Gateway resources (all namespaces) ---'
        kubectl get gateway -A 2>/dev/null || echo '(none)'
        echo ''

        echo '--- HTTPRoute resources (all namespaces) ---'
        kubectl get httproute -A 2>/dev/null || echo '(none)'
        echo ''

        echo '--- DestinationRules (all namespaces) ---'
        kubectl get destinationrule -A 2>/dev/null || echo '(none)'
        echo ''

        echo '--- Istio proxy status ---'
        istioctl proxy-status 2>/dev/null || echo '(no proxies)'
        echo ''

        echo '--- Namespaces with sidecar injection ---'
        kubectl get namespaces -l istio-injection=enabled 2>/dev/null || echo '(none)'
    "
}

# ============================================================================
# Cleanup
# ============================================================================
cleanup_istio() {
    banner "Removing Istio + Gateway API"
    warn "This will uninstall Istio and remove Gateway API CRDs."
    warn "Any app namespaces using the mesh should be cleaned up first."
    echo ""

    remote "$MASTER_HOST" "
        ISTIO_NAMESPACE='${ISTIO_NAMESPACE}'

        echo '--- Uninstalling Istio ---'
        if command -v istioctl &>/dev/null; then
            istioctl uninstall --purge -y 2>/dev/null || true
        fi

        echo ''
        echo '--- Removing Istio namespace ---'
        kubectl delete namespace \${ISTIO_NAMESPACE} 2>/dev/null || true

        echo ''
        echo '--- Removing Gateway API CRDs ---'
        GATEWAY_API_VERSION='${GATEWAY_API_VERSION}'
        kubectl delete -f https://github.com/kubernetes-sigs/gateway-api/releases/download/\${GATEWAY_API_VERSION}/experimental-install.yaml 2>/dev/null || true

        echo ''
        echo '--- Removing istioctl binary ---'
        sudo rm -f /usr/local/bin/istioctl
        rm -rf istio-* 2>/dev/null || true

        echo ''
        echo 'Cleanup complete.'
        kubectl get namespaces
    "
    ok "Istio + Gateway API removed"
}

# ============================================================================
# Full deployment
# ============================================================================
full_deploy() {
    info "Starting Istio + Gateway API deployment on master (${MASTER_HOST})"
    echo ""

    # Create log directory
    mkdir -p "${LOG_DIR}"

    step1_install_istioctl         2>&1 | tee -a "${LOG_DIR}/istio-deploy.log"
    step2_install_gateway_api_crds 2>&1 | tee -a "${LOG_DIR}/istio-deploy.log"
    step3_install_istio            2>&1 | tee -a "${LOG_DIR}/istio-deploy.log"
    step4_verify                   2>&1 | tee -a "${LOG_DIR}/istio-deploy.log"

    banner "Istio + Gateway API deployment complete!"
    info "Logs saved to ${LOG_DIR}/istio-deploy.log"
    info ""
    info "The platform is ready. Istio auto-created the 'istio' GatewayClass."
    info ""
    info "Next steps:"
    echo "  1. Run: ./deploy-app.sh demo           Deploy canary routing demo"
    echo "          ./deploy-app.sh retry-budget   Deploy retry budget demo (GEP-3388)"
    echo "  2. Run: ./deploy-istio.sh --status     Check platform status"
    echo ""
    echo "  Or deploy your own app:"
    echo "    - Create a namespace with label: istio-injection=enabled"
    echo "    - Deploy your app (sidecars auto-injected)"
    echo "    - Create a Gateway (gatewayClassName: istio) in that namespace"
    echo "    - Create HTTPRoute(s) to route traffic to your services"
}

# ============================================================================
# CLI
# ============================================================================
main() {
    case "${1:-}" in
        --step)
            local step="${2:?'Provide a step number (1-4)'}"
            case "$step" in
                1) step1_install_istioctl ;;
                2) step2_install_gateway_api_crds ;;
                3) step3_install_istio ;;
                4) step4_verify ;;
                *) err "Unknown step: $step (valid: 1-4)"; exit 1 ;;
            esac
            ;;
        --status)
            show_status
            ;;
        --cleanup)
            cleanup_istio
            ;;
        --help|-h)
            echo "Usage: $0 [OPTION]"
            echo ""
            echo "Installs the Istio service mesh + Kubernetes Gateway API on your cluster."
            echo "Run this AFTER deploy-k8s.sh. Then use deploy-app.sh <app> for applications."
            echo ""
            echo "Options:"
            echo "  (none)       Full install (steps 1-4)"
            echo "  --step N     Run only step N (1-4)"
            echo "  --status     Show Istio & Gateway API status"
            echo "  --cleanup    Remove Istio + Gateway API CRDs"
            echo "  --help       Show this help"
            echo ""
            echo "Steps:"
            echo "  1  Install istioctl on master"
            echo "  2  Install Gateway API CRDs (${GATEWAY_API_VERSION})"
            echo "  3  Install Istio control plane (${ISTIO_PROFILE} profile)"
            echo "  4  Verify GatewayClass + installation"
            echo ""
            echo "Configuration: edit k8s-config.sh (Istio section)"
            ;;
        "")
            full_deploy
            ;;
        *)
            err "Unknown option: $1 (try --help)"
            exit 1
            ;;
    esac
}

main "$@"
