# Kubernetes Cluster Deployment

Automated deployment of a Kubernetes cluster on remote Ubuntu 22.04 nodes using kubeadm. Runs entirely from your local machine over SSH.

## Prerequisites

- Ubuntu 22.04 on all remote nodes
- SSH access from your local machine to every node (password-less recommended)
- `sudo` privileges on the remote nodes

## Quick Start

1. **Edit the config** — open `k8s-config.sh` and set your nodes:

   ```bash
   SSH_USER="yazhuoz"
   MASTER_HOST="pc749.emulab.net"
   WORKER_HOSTS=("pc751.emulab.net")
   ```

2. **Deploy:**

   ```bash
   ./deploy-k8s.sh
   ```

3. **Copy kubeconfig locally** (optional):

   ```bash
   scp yazhuoz@pc749.emulab.net:~/.kube/config ~/.kube/config
   ```

## Files

| File | Purpose |
|------|---------|
| `k8s-config.sh` | All configurable variables (nodes, versions, CNI, etc.) |
| `deploy-k8s.sh` | Main deployment script |
| `k8s-deploy-logs/` | Per-node logs (created at runtime) |

## What It Does

| Step | Description | Runs on |
|------|-------------|---------|
| 1 | Set hostnames | All nodes |
| 2 | Update `/etc/hosts` | All nodes |
| 3 | Disable swap & configure kernel | All nodes |
| 4 | Install containerd + crictl | All nodes |
| 5 | Install kubeadm, kubelet, kubectl | All nodes |
| 6 | Initialize cluster (`kubeadm init`) | Master |
| 7 | Join workers to cluster | Workers |
| 8 | Install CNI plugin (Calico/Flannel) | Master |
| 9 | Install Metrics Server | Master |
| 10 | Verify cluster | Master |

## Usage

```bash
./deploy-k8s.sh              # Full deploy
./deploy-k8s.sh --step 4     # Run only step 4 on all nodes
./deploy-k8s.sh --master     # Deploy master only
./deploy-k8s.sh --workers    # Deploy workers only
./deploy-k8s.sh --join       # Print the kubeadm join command
./deploy-k8s.sh --verify     # Check cluster status
./deploy-k8s.sh --cleanup    # Tear down cluster & remove all packages
```

## Adding More Workers

Add entries to the arrays in `k8s-config.sh`:

```bash
WORKER_HOSTS=("pc751.emulab.net" "pc752.emulab.net")
WORKER_HOSTNAMES=("worker01" "worker02")
```

Then re-run `./deploy-k8s.sh`.

## Cleanup

To completely tear down the cluster and remove all Kubernetes packages:

```bash
./deploy-k8s.sh --cleanup
```

This resets kubeadm, purges packages, removes configs, cleans up CNI interfaces, and flushes iptables rules on every node.
