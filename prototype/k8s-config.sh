#!/usr/bin/env bash
# ============================================================================
# Kubernetes Cluster Deployment - Configuration
# ============================================================================
# Edit the variables below to match your environment.
# This file is sourced by deploy-k8s.sh — do NOT execute it directly.
# ============================================================================

# ---------- SSH settings ----------
SSH_USER="lcresci"
SSH_KEY=""                          # e.g. "~/.ssh/id_rsa" (leave empty to use default)
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o LogLevel=ERROR"

# ---------- Node definitions ----------
# Current CloudLab/Emulab allocation (UBUNTU24-64-STD, d430 hardware).
#
# Logical  | Emulab host         | Role
# ---------+---------------------+---------------------------------------
# node0    | pc835.emulab.net    | Kubernetes master
# node1    | pc822.emulab.net    | Worker 1
# node2    | pc832.emulab.net    | Worker 2
# node3    | pc830.emulab.net    | Worker 3
# node4    | pc829.emulab.net    | Worker 4
# node5    | pc827.emulab.net    | External load-gen / client host
#
# Note: nodes are running Ubuntu 24.04 (the older allocation used 22.04).
# If kubeadm / containerd steps fail, verify deploy-k8s.sh package pins.

# Master node
MASTER_HOST="pc835.emulab.net"      # SSH-reachable address
MASTER_HOSTNAME="master-node"       # Hostname to set on the machine
MASTER_IP="10.10.1.1"               # Private Emulab fabric IP (canonical path)

# Worker nodes — add more entries to scale out
WORKER_HOSTS=("pc822.emulab.net" "pc832.emulab.net" "pc830.emulab.net" "pc829.emulab.net")
WORKER_HOSTNAMES=("worker01" "worker02" "worker03" "worker04")
WORKER_IPS=()                       # (optional) same length as WORKER_HOSTS, or leave empty

# Client node (external load generator — not part of K8s cluster)
CLIENT_HOST="pc827.emulab.net"      # SSH-reachable address

# ---------- Kubernetes settings ----------
K8S_VERSION="v1.30"                 # Kubernetes APT repo channel
POD_NETWORK_CIDR="10.244.0.0/16"   # Pod network CIDR (must match CNI plugin config)
CNI_PLUGIN="calico"                 # "calico" or "flannel"

# Calico manifest URL (used when CNI_PLUGIN=calico)
CALICO_MANIFEST="https://raw.githubusercontent.com/projectcalico/calico/v3.29.2/manifests/calico.yaml"

# ---------- Container runtime ----------
CRICTL_VERSION="v1.35.0"

# ---------- Istio + Gateway API ----------
ISTIO_VERSION="1.27.5"             # Istio release version (1.27+ for Gateway API v1.3 experimental conformance)
GATEWAY_API_VERSION="v1.3.0"       # Kubernetes Gateway API CRD version
ISTIO_PROFILE="default"            # "default" = istiod + ingress gateway; "minimal" = istiod only
ISTIO_NAMESPACE="istio-system"     # Namespace for Istio control plane
ISTIO_TEST_NS="istio-test"         # Namespace for demo app + Gateway (with sidecar injection)

# ---------- Kubeconfig ----------
KUBECONFIG_PATH="$HOME/.kube/config-primary"   # Cluster-specific kubeconfig file
export KUBECONFIG="$KUBECONFIG_PATH"

# ---------- Misc ----------
LOG_DIR="./k8s-deploy-logs"         # Local directory for per-node log files
