# It's Time to Retry

This repository contains:

- a discrete-event simulator for retry behavior and global retry budgets in microservices
- a Kubernetes/Istio prototype (Emulab/CloudLab) for live experiments, including the **Arolla** Envoy WASM filter — a goodput-coupled retry budget with two-level admission control

## Repository Layout

- `simulator/`
  - Discrete-event simulator (M/G/c/K queues, retry policies, AIMD + Arolla budgets)
  - Main docs: [`simulator/README.md`](simulator/README.md)
- `prototype/`
  - Kubernetes + Istio deployment scripts, the Arolla WASM filter (`arolla-filter/`, `arolla-filter-fairness/`), policy manifests, and the experiment harness in `experiments/`
  - Main docs: [`prototype/README.md`](prototype/README.md)
- `outputs/`
  - Local experiment outputs / generated artifacts

## Simulator

```bash
cd simulator
pip install -e .
python bin/workflow.py experiments/yaml/default.yaml
```

Results land in `results/<config>_<timestamp>/` (CSV + plots). See [`simulator/README.md`](simulator/README.md) for sweep configs, multi-client mode, and AIMD/Arolla retry-budget YAML.

## Prototype (Kubernetes/Istio)

The prototype runs on Emulab/CloudLab. Prerequisites (SSH access, Rust + `wasm32-wasip1`, Ubuntu 24.04 d430 nodes) are listed in [`prototype/README.md`](prototype/README.md).

**1. Bring up the cluster and deploy online-boutique**

```bash
cd prototype

# Configure your Emulab nodes
vi k8s-config.sh

# Kubernetes + Istio (Gateway API)
./deploy-k8s.sh
./deploy-istio.sh

# Deploy and verify online-boutique
./deploy-app.sh online-boutique
./deploy-app.sh online-boutique --test
```

**2. Apply a retry-control policy**

```bash
# Build, upload, and serve the Arolla WASM filter
./deploy-policy.sh build-wasm
./deploy-policy.sh upload-wasm
./deploy-policy.sh serve-wasm

# Apply one of: arolla | arolla-fairness | circuit-breaker | envoy-retry-budget | no-control
./deploy-policy.sh apply arolla
./deploy-policy.sh status
```

**3. Run external retry-study clients on `CLIENT_HOST`**

These clients run outside Kubernetes (on `CLIENT_HOST` from `prototype/k8s-config.sh`) and target the online-boutique Gateway.

```bash
cd prototype/clients/online-boutique
./run-clients.sh start
./run-clients.sh status
./run-clients.sh logs
./run-clients.sh fetch-metrics
./run-clients.sh stop

# Or limit to selected profiles
PROFILES=checkout,cart-stress ./run-clients.sh start
```

**4. Run a paper experiment (orchestrated)**

The orchestrator drives phases (warmup → prefault → fault → recovery → cooldown) across multiple policies and client profiles, collecting per-policy metrics:

```bash
# Scale up for the metastable-failure scenario
./deploy-cluster-profile.sh 2-replica

# One experiment, four policies, one fault profile
NUM_LOADERS=4 experiments/run-experiment.sh \
  --policies no-control,circuit-breaker,envoy-retry-budget,arolla \
  --client-profiles post-cart-stress-open \
  -F cartservice-100pct \
  --warmup 30 --prefault 60 --fault 10 --recovery 60 --cooldown 10
```

Results land in `outputs/prototype/<client-profile>/<client-profile>_<timestamp>/`. For parameter sweeps, grid sweeps, the `paper.sh` dispatcher, and analysis/plotting, see [`prototype/README.md`](prototype/README.md) and `prototype/experiments/`.

## Notes

- The prototype `online-boutique` app disables the stock in-cluster loadgenerator by default in this repo and uses external controllable clients instead.
- External client profiles are "AWS-SDK-style" retry behaviors (not literal AWS SDK calls), because `online-boutique` is a generic HTTP application.