#!/usr/bin/env bash
# ============================================================================
# Kubernetes Cluster Deployment Script
# ============================================================================
# Runs LOCALLY and configures remote nodes over SSH.
#
# Usage:
#   ./deploy-k8s.sh              # Full deploy (all steps)
#   ./deploy-k8s.sh --step 4     # Run only step 4 on all nodes (1-10)
#   ./deploy-k8s.sh --master     # Run full deploy on master only
#   ./deploy-k8s.sh --workers    # Run full deploy on workers only
#   ./deploy-k8s.sh --join       # Re-print / re-run the join command
#   ./deploy-k8s.sh --cleanup    # Tear down everything
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

# Run a command on remote and capture output (no prefix)
remote_capture() {
    local host="$1"; shift
    eval "$(ssh_cmd "$host")" "bash -s" <<-REMOTE_EOF 2>&1
$@
REMOTE_EOF
}

# ── Resolve IPs if not provided ─────────────────────────────────────────────
resolve_ips() {
    if [[ -z "${MASTER_IP}" ]]; then
        info "Resolving master IP from ${MASTER_HOST}…"
        MASTER_IP=$(remote_capture "$MASTER_HOST" "hostname -I | awk '{print \$1}'")
        MASTER_IP=$(echo "$MASTER_IP" | tr -d '[:space:]')
        ok "Master IP: ${MASTER_IP}"
    fi
    for i in "${!WORKER_HOSTS[@]}"; do
        if [[ -z "${WORKER_IPS[$i]:-}" ]]; then
            info "Resolving worker IP from ${WORKER_HOSTS[$i]}…"
            WORKER_IPS[$i]=$(remote_capture "${WORKER_HOSTS[$i]}" "hostname -I | awk '{print \$1}'")
            WORKER_IPS[$i]=$(echo "${WORKER_IPS[$i]}" | tr -d '[:space:]')
            ok "Worker ${WORKER_HOSTNAMES[$i]} IP: ${WORKER_IPS[$i]}"
        fi
    done
}

# ── Build /etc/hosts entries ────────────────────────────────────────────────
build_hosts_entries() {
    local entries=""
    entries+="${MASTER_IP} ${MASTER_HOSTNAME}\n"
    for i in "${!WORKER_HOSTS[@]}"; do
        entries+="${WORKER_IPS[$i]} ${WORKER_HOSTNAMES[$i]}\n"
    done
    echo -e "$entries"
}

# ── Step functions ───────────────────────────────────────────────────────────

# ---------- Common steps (run on ALL nodes) ----------

step1_set_hostname() {
    local host="$1" desired_hostname="$2"
    banner "Step 1: Set hostname on ${host} → ${desired_hostname}"
    remote "$host" "
        sudo hostnamectl set-hostname '${desired_hostname}'
        echo 'Hostname set to:' \$(hostname)
    "
    ok "Hostname configured on ${host}"
}

step2_update_hosts() {
    local host="$1"
    local entries
    entries=$(build_hosts_entries)
    banner "Step 2: Update /etc/hosts on ${host}"
    remote "$host" "
        # Remove any previous k8s-deploy entries
        sudo sed -i '/# k8s-deploy-managed/d' /etc/hosts
        # Append new entries
        echo '${entries}' | while IFS= read -r line; do
            [[ -n \"\$line\" ]] && echo \"\${line}  # k8s-deploy-managed\" | sudo tee -a /etc/hosts > /dev/null
        done
        echo '--- /etc/hosts ---'
        cat /etc/hosts
    "
    ok "/etc/hosts updated on ${host}"
}

step3_disable_swap_and_kernel() {
    local host="$1"
    banner "Step 3: Disable swap & configure kernel on ${host}"
    remote "$host" '
        # Disable swap
        sudo swapoff -a
        sudo sed -i "/ swap / s/^\(.*\)$/#\1/g" /etc/fstab

        # Kernel modules
        cat <<MODEOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
MODEOF
        sudo modprobe overlay
        sudo modprobe br_netfilter

        # Sysctl parameters
        cat <<SYSEOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
SYSEOF
        sudo sysctl --system
        echo "Swap disabled, kernel modules loaded, sysctl applied."
    '
    ok "Kernel configuration done on ${host}"
}

step4_install_containerd() {
    local host="$1"
    banner "Step 4: Install containerd on ${host}"
    remote "$host" "
        export DEBIAN_FRONTEND=noninteractive

        # Dependencies
        sudo apt-get update -qq
        sudo apt-get install -y -qq curl software-properties-common apt-transport-https ca-certificates gnupg

        # Docker GPG key & repo (for containerd.io)
        sudo mkdir -p /etc/apt/trusted.gpg.d
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmour --yes -o /etc/apt/trusted.gpg.d/docker.gpg
        sudo add-apt-repository -y \"deb [arch=amd64] https://download.docker.com/linux/ubuntu \$(lsb_release -cs) stable\"
        sudo apt-get update -qq

        # Install containerd
        sudo apt-get install -y -qq containerd.io

        # Configure systemd cgroup driver
        sudo mkdir -p /etc/containerd
        containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
        sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml

        # Restart containerd
        sudo systemctl restart containerd
        sudo systemctl enable containerd

        # Install crictl
        CRICTL_VERSION='${CRICTL_VERSION}'
        curl -sLO https://github.com/kubernetes-sigs/cri-tools/releases/download/\${CRICTL_VERSION}/crictl-\${CRICTL_VERSION}-linux-amd64.tar.gz
        sudo tar zxf crictl-\${CRICTL_VERSION}-linux-amd64.tar.gz -C /usr/local/bin
        rm -f crictl-\${CRICTL_VERSION}-linux-amd64.tar.gz

        # Configure crictl
        cat <<CRIEOF | sudo tee /etc/crictl.yaml
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 10
debug: false
CRIEOF

        echo 'containerd & crictl installed.'
        sudo systemctl status containerd --no-pager -l || true
    "
    ok "Containerd installed on ${host}"
}

step5_install_k8s_components() {
    local host="$1"
    banner "Step 5: Install kubeadm, kubelet, kubectl on ${host}"
    remote "$host" "
        export DEBIAN_FRONTEND=noninteractive

        # Clean up any stale Kubernetes APT sources
        sudo rm -f /etc/apt/sources.list.d/archive_uri-https_apt_kubernetes_io-*.list 2>/dev/null || true
        # Also remove any old kubernetes-xenial entries
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
            [ -f \"\$f\" ] && sudo sed -i '/kubernetes-xenial/d; /apt\.kubernetes\.io/d; /packages\.cloud\.google\.com\/apt/d' \"\$f\" 2>/dev/null || true
        done

        # Add new Kubernetes repo
        sudo mkdir -p /etc/apt/keyrings
        curl -fsSL https://pkgs.k8s.io/core:/stable:/${K8S_VERSION}/deb/Release.key | sudo gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
        echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${K8S_VERSION}/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list

        sudo apt-get update -qq
        sudo apt-get install -y -qq kubelet kubeadm kubectl
        sudo apt-mark hold kubelet kubeadm kubectl

        echo 'Kubernetes components installed:'
        kubeadm version
        kubectl version --client
        kubelet --version
    "
    ok "K8s components installed on ${host}"
}

# ---------- Master-only steps ----------

step6_init_cluster() {
    banner "Step 6: Initialize Kubernetes cluster on master (${MASTER_HOST})"
    remote "$MASTER_HOST" "
        # Reset if previously initialized (idempotent)
        sudo kubeadm reset -f 2>/dev/null || true

        sudo kubeadm init --pod-network-cidr='${POD_NETWORK_CIDR}' | tee /tmp/kubeadm-init.log

        # Set up kubeconfig for the SSH user
        mkdir -p \$HOME/.kube
        sudo cp -f /etc/kubernetes/admin.conf \$HOME/.kube/config
        sudo chown \$(id -u):\$(id -g) \$HOME/.kube/config

        echo 'Cluster initialized. Extracting join command…'
        grep -A1 'kubeadm join' /tmp/kubeadm-init.log || true
    "
    ok "Cluster initialized on ${MASTER_HOST}"
}

step7_get_join_command() {
    banner "Step 7: Retrieve join command from master"
    JOIN_CMD=$(remote_capture "$MASTER_HOST" "kubeadm token create --print-join-command")
    JOIN_CMD=$(echo "$JOIN_CMD" | grep "kubeadm join" | head -1)
    if [[ -z "$JOIN_CMD" ]]; then
        err "Failed to retrieve join command from master!"
        exit 1
    fi
    ok "Join command: ${JOIN_CMD}"
}

step7_join_workers() {
    step7_get_join_command
    for i in "${!WORKER_HOSTS[@]}"; do
        local host="${WORKER_HOSTS[$i]}"
        banner "Step 7: Join worker ${WORKER_HOSTNAMES[$i]} (${host}) to cluster"
        remote "$host" "
            sudo kubeadm reset -f 2>/dev/null || true
            sudo ${JOIN_CMD}
        "
        ok "Worker ${WORKER_HOSTNAMES[$i]} joined"
    done
}

step8_install_cni() {
    banner "Step 8: Install CNI plugin (${CNI_PLUGIN}) on master"
    if [[ "${CNI_PLUGIN}" == "calico" ]]; then
        remote "$MASTER_HOST" "
            kubectl apply -f '${CALICO_MANIFEST}'
            echo 'Waiting for Calico pods to start…'
            sleep 10
            kubectl get pods -n kube-system
        "
    elif [[ "${CNI_PLUGIN}" == "flannel" ]]; then
        remote "$MASTER_HOST" "
            kubectl apply -f https://raw.githubusercontent.com/flannel-io/flannel/master/Documentation/kube-flannel.yml
            echo 'Waiting for Flannel pods to start…'
            sleep 10
            kubectl get pods -n kube-system
        "
    else
        err "Unknown CNI plugin: ${CNI_PLUGIN}"
        exit 1
    fi
    ok "CNI plugin ${CNI_PLUGIN} deployed"
}

step9_install_metrics_server() {
    banner "Step 9: Install Metrics Server on master"
    remote "$MASTER_HOST" "
        echo 'Installing Metrics Server (with --kubelet-insecure-tls for local setups)…'
        kubectl apply -f https://raw.githubusercontent.com/techiescamp/cka-certification-guide/refs/heads/main/lab-setup/manifests/metrics-server/metrics-server.yaml

        echo 'Waiting for metrics server to become ready (up to 90s)…'
        kubectl -n kube-system rollout status deployment/metrics-server --timeout=90s || true

        echo ''
        echo 'Testing metrics API…'
        sleep 15
        kubectl top nodes || echo '(metrics may take another minute to become available)'
        echo ''
        kubectl top pod -n kube-system || true
    "
    ok "Metrics Server installed"
}

step10_verify() {
    banner "Step 10: Verify cluster"
    remote "$MASTER_HOST" "
        echo '--- Nodes ---'
        kubectl get nodes -o wide
        echo ''
        echo '--- System pods ---'
        kubectl get pods -n kube-system
        echo ''
        echo '--- Node metrics ---'
        kubectl top nodes || true
        echo ''
        echo '--- Cluster info ---'
        kubectl cluster-info
    "
    ok "Cluster verification complete"
}

# ── Cleanup ──────────────────────────────────────────────────────────────────
cleanup_node() {
    local host="$1"
    banner "Cleaning up ${host}"
    remote "$host" '
        # Reset kubeadm (drains, removes etcd, certs, etc.)
        sudo kubeadm reset -f 2>/dev/null || true

        # Remove Kubernetes packages
        sudo apt-mark unhold kubelet kubeadm kubectl 2>/dev/null || true
        sudo apt-get purge -y kubelet kubeadm kubectl 2>/dev/null || true

        # Remove containerd
        sudo systemctl stop containerd 2>/dev/null || true
        sudo apt-get purge -y containerd.io 2>/dev/null || true

        # Autoremove leftover dependencies
        sudo apt-get autoremove -y 2>/dev/null || true

        # Clean up config files and directories
        sudo rm -rf /etc/kubernetes
        sudo rm -rf /var/lib/kubelet
        sudo rm -rf /var/lib/etcd
        sudo rm -rf $HOME/.kube
        sudo rm -rf /etc/cni/net.d
        sudo rm -rf /opt/cni
        sudo rm -rf /var/lib/cni
        sudo rm -rf /etc/containerd
        sudo rm -f /etc/crictl.yaml
        sudo rm -f /usr/local/bin/crictl

        # Remove APT repo entries
        sudo rm -f /etc/apt/sources.list.d/kubernetes.list
        sudo rm -f /etc/apt/keyrings/kubernetes-apt-keyring.gpg
        sudo rm -f /etc/apt/trusted.gpg.d/docker.gpg
        # Remove Docker repo (for containerd)
        sudo add-apt-repository --remove "deb [arch=amd64] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" 2>/dev/null || true

        # Clean up network interfaces created by CNI
        sudo ip link delete cali+ 2>/dev/null || true
        sudo ip link delete tunl0 2>/dev/null || true
        sudo ip link delete vxlan.calico 2>/dev/null || true
        sudo ip link delete flannel.1 2>/dev/null || true
        sudo ip link delete cni0 2>/dev/null || true

        # Remove iptables rules added by Kubernetes
        sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X 2>/dev/null || true
        sudo ipvsadm --clear 2>/dev/null || true

        # Remove /etc/hosts entries we added
        sudo sed -i "/# k8s-deploy-managed/d" /etc/hosts

        # Remove kernel module configs we added
        sudo rm -f /etc/modules-load.d/k8s.conf
        sudo rm -f /etc/sysctl.d/k8s.conf
        sudo sysctl --system >/dev/null 2>&1 || true

        echo "Cleanup complete on $(hostname)"
    '
    ok "Cleanup done on ${host}"
}

cleanup_cluster() {
    banner "Full cluster cleanup"
    warn "This will completely tear down Kubernetes and remove all related packages from ALL nodes."
    echo ""

    # Clean workers first, then master
    for i in "${!WORKER_HOSTS[@]}"; do
        cleanup_node "${WORKER_HOSTS[$i]}"
    done
    cleanup_node "$MASTER_HOST"

    ok "All nodes cleaned up. Cluster has been fully removed."
}

# ── Run common steps on a single node ───────────────────────────────────────
run_common_steps() {
    local host="$1" hostname="$2"
    step1_set_hostname  "$host" "$hostname"
    step2_update_hosts  "$host"
    step3_disable_swap_and_kernel "$host"
    step4_install_containerd "$host"
    step5_install_k8s_components "$host"
}

# ── Full deployment ─────────────────────────────────────────────────────────
full_deploy() {
    info "Starting full Kubernetes cluster deployment"
    info "Master: ${SSH_USER}@${MASTER_HOST} → ${MASTER_HOSTNAME}"
    for i in "${!WORKER_HOSTS[@]}"; do
        info "Worker: ${SSH_USER}@${WORKER_HOSTS[$i]} → ${WORKER_HOSTNAMES[$i]}"
    done
    echo ""

    # Resolve IPs
    resolve_ips

    # Create log directory
    mkdir -p "${LOG_DIR}"

    # Steps 1-5: common setup on ALL nodes
    banner "Configuring master node"
    run_common_steps "$MASTER_HOST" "$MASTER_HOSTNAME" 2>&1 | tee "${LOG_DIR}/master.log"

    for i in "${!WORKER_HOSTS[@]}"; do
        banner "Configuring worker: ${WORKER_HOSTNAMES[$i]}"
        run_common_steps "${WORKER_HOSTS[$i]}" "${WORKER_HOSTNAMES[$i]}" 2>&1 | tee "${LOG_DIR}/${WORKER_HOSTNAMES[$i]}.log"
    done

    # Step 6: Initialize cluster on master
    step6_init_cluster 2>&1 | tee -a "${LOG_DIR}/master.log"

    # Step 7: Join workers
    step7_join_workers 2>&1 | tee -a "${LOG_DIR}/master.log"

    # Step 8: Install CNI
    step8_install_cni 2>&1 | tee -a "${LOG_DIR}/master.log"

    # Step 9: Install Metrics Server
    step9_install_metrics_server 2>&1 | tee -a "${LOG_DIR}/master.log"

    # Step 10: Verify
    step10_verify

    banner "Deployment complete!"
    info "Logs saved to ${LOG_DIR}/"
    info "To access the cluster from your local machine, copy the kubeconfig:"
    echo "  scp ${SSH_USER}@${MASTER_HOST}:~/.kube/config ~/.kube/config"
}

# ── CLI argument handling ───────────────────────────────────────────────────
main() {
    case "${1:-}" in
        --step)
            resolve_ips
            local step="${2:?'Provide a step number (1-10)'}"
            local all_hosts=("$MASTER_HOST")
            local all_hostnames=("$MASTER_HOSTNAME")
            for i in "${!WORKER_HOSTS[@]}"; do
                all_hosts+=("${WORKER_HOSTS[$i]}")
                all_hostnames+=("${WORKER_HOSTNAMES[$i]}")
            done
            case "$step" in
                1) for i in "${!all_hosts[@]}"; do step1_set_hostname "${all_hosts[$i]}" "${all_hostnames[$i]}"; done ;;
                2) for h in "${all_hosts[@]}"; do step2_update_hosts "$h"; done ;;
                3) for h in "${all_hosts[@]}"; do step3_disable_swap_and_kernel "$h"; done ;;
                4) for h in "${all_hosts[@]}"; do step4_install_containerd "$h"; done ;;
                5) for h in "${all_hosts[@]}"; do step5_install_k8s_components "$h"; done ;;
                6) step6_init_cluster ;;
                7) step7_join_workers ;;
                8) step8_install_cni ;;
                9) step9_install_metrics_server ;;
                10) step10_verify ;;
                *) err "Unknown step: $step (valid: 1-10)"; exit 1 ;;
            esac
            ;;
        --master)
            resolve_ips
            run_common_steps "$MASTER_HOST" "$MASTER_HOSTNAME"
            step6_init_cluster
            step8_install_cni
            step9_install_metrics_server
            step10_verify
            ;;
        --workers)
            resolve_ips
            step7_get_join_command
            for i in "${!WORKER_HOSTS[@]}"; do
                run_common_steps "${WORKER_HOSTS[$i]}" "${WORKER_HOSTNAMES[$i]}"
                remote "${WORKER_HOSTS[$i]}" "
                    sudo kubeadm reset -f 2>/dev/null || true
                    sudo ${JOIN_CMD}
                "
                ok "Worker ${WORKER_HOSTNAMES[$i]} joined"
            done
            step10_verify
            ;;
        --join)
            step7_get_join_command
            echo ""
            echo "$JOIN_CMD"
            ;;
        --verify)
            step10_verify
            ;;
        --cleanup)
            cleanup_cluster
            ;;
        --help|-h)
            echo "Usage: $0 [OPTION]"
            echo ""
            echo "Options:"
            echo "  (none)        Full deployment of K8s cluster"
            echo "  --step N      Run only step N (1-10) on all nodes"
            echo "  --master      Deploy master node only (steps 1-6, 8-10)"
            echo "  --workers     Deploy worker nodes only (steps 1-5, join)"
            echo "  --join        Print the kubeadm join command"
            echo "  --verify      Verify cluster status"
            echo "  --cleanup     Tear down cluster & remove all K8s packages"
            echo "  --help        Show this help"
            echo ""
            echo "Steps:"
            echo "  1  Set hostnames"
            echo "  2  Update /etc/hosts"
            echo "  3  Disable swap & configure kernel"
            echo "  4  Install containerd + crictl"
            echo "  5  Install kubeadm, kubelet, kubectl"
            echo "  6  Initialize cluster (master only)"
            echo "  7  Join workers to cluster"
            echo "  8  Install CNI plugin"
            echo "  9  Install Metrics Server"
            echo "  10 Verify cluster"
            echo ""
            echo "Configuration: edit k8s-config.sh"
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
