# Prototype — Kubernetes/Istio Experiment Infrastructure

## Prerequisites
- SSH access from your local machine to all Emulab nodes (passwordless recommended)
- `sudo` privileges on the remote nodes
- Rust + `wasm32-wasip1` target for building the Arolla filter
- Ubuntu 24.04 on all nodes (d430 hardware on CloudLab/Emulab)

## Quick Start

```bash
cd prototype

# 1. Configure your Emulab nodes
vi k8s-config.sh

# 2. Deploy Kubernetes cluster
./deploy-k8s.sh

# 3. Deploy Istio + Gateway API
./deploy-istio.sh

# 4. Deploy Online Boutique
./deploy-app.sh online-boutique

# 5. Verify
./deploy-app.sh online-boutique --test
```

### Running with the Arolla filter
Before running experiments, the Wasm binary must be built, uploaded, and served:
```bash
# 6. Build the Arolla Wasm filter (requires Rust)
./deploy-policy.sh build-wasm

# 7. Upload to master node
./deploy-policy.sh upload-wasm

# 8. Start HTTP server to serve the binary to sidecars
./deploy-policy.sh serve-wasm

# 9. Apply the Arolla policy
./deploy-policy.sh apply arolla
./deploy-policy.sh status
```

### Reproduce metastable failure experiments

```bash
# scale up to 2 replicas
./prototype/deploy-cluster-profile.sh 2-replica

# run the experiment with 4 loaders, testing 4 policies, and using the post-cart-stress-open client profile, and the cartservice-100pct fault profile
NUM_LOADERS=4 prototype/experiments/run-experiment.sh \
  --policies no-control,circuit-breaker,envoy-retry-budget,arolla \
  --client-profiles post-cart-stress-open \
  -F cartservice-100pct \
  --warmup 30 --prefault 60 --fault 10 --recovery 60 --cooldown 10
```

The results including the raw data and plots will be stored in `outputs/prototype/{$client-profile}/{$client-profile}_{$timestamp}/`. 

## Troubleshooting

Run the following commands in Bash on the evaluator's machine, from `prototype/`.
Source the configuration to select this cluster's kubeconfig:

```bash
source k8s-config.sh
command -v kubectl
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get events -A --sort-by=.metadata.creationTimestamp
```

Expect five nodes `Ready`, five `calico-node` pods `Running` and ready (`1/1`),
and a ready `istiod` deployment. `deploy-app.sh` checks these before deploying.
An application rollout timeout alone does not identify the underlying failure.
Use pod events to distinguish scheduling, image-download, networking, and
Istio webhook failures ([Kubernetes debugging guide](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)).

### Local access or missing nodes

If kubectl is missing, install it locally using the prerequisite link above.
If local access fails, first check access directly on the configured master:

```bash
ssh "${SSH_USER}@${MASTER_HOST}" 'kubectl get nodes -o wide'
```

If this works, refresh the local kubeconfig; this replaces the file at
`KUBECONFIG_PATH`, so confirm it belongs to this experiment:

```bash
mkdir -p "$(dirname "$KUBECONFIG_PATH")"
scp "${SSH_USER}@${MASTER_HOST}:~/.kube/config" "$KUBECONFIG_PATH"
kubectl get nodes -o wide
```

If remote access also fails, inspect the provisioning logs in `k8s-deploy-logs/`
and kubelet/containerd logs on the affected host:

```bash
FAILED_HOST="${WORKER_HOSTS[0]}"  # Replace with the affected worker or MASTER_HOST.
ssh "${SSH_USER}@${FAILED_HOST}" \
  'sudo journalctl -u kubelet -u containerd --since "20 minutes ago" --no-pager'
```

Compare configured hostnames with `kubectl get nodes`. For a worker that never
joined, `./deploy-k8s.sh --join` prints a fresh join command; run that command
with `sudo` on the unjoined worker after resolving its setup errors.


### Resume a partial deployment

Fix the first failing component before continuing:

| Failure | Recovery |
|---|---|
| Calico was never installed | With the API reachable, run `./deploy-k8s.sh --step 8`, then verify Calico readiness. |
| Istio missing or installation incomplete | Once nodes and Calico are healthy, rerun `./deploy-istio.sh`; inspect `kubectl -n istio-system describe pod <pod>` for remaining failures. |
| Application missing or rollout incomplete | Once preflight passes, rerun `./deploy-app.sh online-boutique`, then `./deploy-cluster-profile.sh 2-replica`. |
| Pods `Pending` with `Insufficient cpu` or `Insufficient memory` events | Check resource requests and node capacity with `kubectl describe nodes`; use the documented d430 allocation for the paper experiments. |

After repairing Istio, confirm readiness with
`kubectl -n istio-system rollout status deployment/istiod --timeout=180s`.
Then resume the Arolla deployment and smoke test in the
[AE instructions](../AE-instructions.md#223-deploy-online-boutique-and-arolla).

### Clean rebuild

The following removes Kubernetes, workloads, and cluster state on every
configured Kubernetes node. Save diagnostic logs first and verify that
`k8s-config.sh` lists only the intended experiment's hosts. A full deployment
also resets the control plane and workers, so it is not a harmless retry.

```bash
./deploy-k8s.sh --cleanup
./deploy-k8s.sh
```