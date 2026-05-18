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

