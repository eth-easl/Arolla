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
# Current CloudLab/Emulab allocation (d430 hardware, UBUNTU24-64-STD,
# experiment latencymodel/lcresci-305577). Private fabric verified healthy
# (every node can ping every other on 10.10.1.x), so we use the canonical
# private LAN routing path that matches the May-1 baseline.
#
# Logical  | Emulab host         | Public IP        | Private IP   | Role
# ---------+---------------------+------------------+--------------+-------------
# node0    | pc810.emulab.net    | 155.98.36.110    | 10.10.1.1    | K8s master
# node1    | pc817.emulab.net    | 155.98.36.117    | 10.10.1.2    | Worker 1
# node2    | pc820.emulab.net    | 155.98.36.120    | 10.10.1.3    | Worker 2
# node3    | pc809.emulab.net    | 155.98.36.109    | 10.10.1.4    | Worker 3
# node4    | pc801.emulab.net    | 155.98.36.101    | 10.10.1.5    | Worker 4
# node5    | pc808.emulab.net    | 155.98.36.108    | 10.10.1.6    | Load-gen client

# Master node
MASTER_HOST="pc810.emulab.net"      # SSH-reachable address
MASTER_HOSTNAME="master-node"       # Hostname to set on the machine
MASTER_IP="10.10.1.1"               # Private Emulab fabric IP (canonical path)

# Worker nodes — add more entries to scale out
WORKER_HOSTS=("pc817.emulab.net" "pc820.emulab.net" "pc809.emulab.net" "pc801.emulab.net")
WORKER_HOSTNAMES=("worker01" "worker02" "worker03" "worker04")
WORKER_IPS=("10.10.1.2" "10.10.1.3" "10.10.1.4" "10.10.1.5")                       # (optional) same length as WORKER_HOSTS, or leave empty

# Calico autodetection: which host interface BGP should peer over.
# enp6s0f3 is the experimental fabric NIC on this d430 allocation; verified
# bidirectional ICMP between all nodes before deploying.
CALICO_AUTODETECT_INTERFACE="enp6s0f3"

# Client node (external load generator — not part of K8s cluster)
CLIENT_HOST="pc808.emulab.net"      # SSH-reachable address
CLIENT_IP="10.10.1.6"               # Private fabric IP — used by the
                                    # in-cluster RL controller to reach
                                    # traffic_gen.py's /window endpoint.
                                    # Pods on the worker network can hit
                                    # this directly via the same 10.10.1.x
                                    # fabric the workers are on.

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
# Intentionally isolated from ~/.kube/config (which may contain other clusters).
# The context is named "emulab" to avoid collisions with any default context name.
# To use kubectl manually: export KUBECONFIG=~/.kube/config-emulab
KUBECONFIG_PATH="$HOME/.kube/config-emulab"    # Cluster-specific kubeconfig file
export KUBECONFIG="$KUBECONFIG_PATH"

# ---------- istiod tuning (Phase 1.3 of plan-12) ----------
# Reduce istiod's debounce window so DestinationRule patches from the RL
# controller propagate to sidecars in ~10ms instead of the default ~100ms.
# This is a cluster-wide change — re-apply after every fresh allocation:
#
#   kubectl -n istio-system set env deployment/istiod PILOT_DEBOUNCE_AFTER=10ms
#   kubectl -n istio-system rollout status deployment/istiod --timeout=60s
#   # verify:
#   kubectl -n istio-system exec deploy/istiod -- env | grep DEBOUNCE
#
# Revert with:
#   kubectl -n istio-system set env deployment/istiod PILOT_DEBOUNCE_AFTER-
#
# Affects every workload in the cluster, so document it on any baseline runs
# you compare against.
ISTIOD_DEBOUNCE_AFTER="10ms"

# ---------- Misc ----------
LOG_DIR="./k8s-deploy-logs"         # Local directory for per-node log files
