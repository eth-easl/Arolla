# It's Time to Retry

This repository contains:

- a discrete-event simulator for retry behavior and global retry budgets in microservices
- a Kubernetes/Istio prototype (Emulab/CloudLab) for live experiments
- postmortem-informed reproductions of real incidents, in both the simulator and on a live Kind cluster

## Repository Layout

- `simulator/`
  - Discrete-event simulator (M/G/c/K queues, retry policies)
  - Main docs: [`simulator/README.md`](simulator/README.md)
- `prototype/`
  - Kubernetes + Istio deployment scripts, the Arolla WASM filter (`arolla-filter/`, `arolla-filter-fairness/`), policy manifests, and the experiment harness in `experiments/`
  - Main docs: [`prototype/README.md`](prototype/README.md)
- `incident-slack-2022-02/`
  - Self-contained Kind reproduction of the Slack 2022-02-22 cache-stampede incident
  - Main docs: [`incident-slack-2022-02/README.md`](incident-slack-2022-02/README.md)
- `outputs/`
  - Local experiment outputs / generated artifacts

## Simulator

```bash
# First time only: create venv and install the package
python3 -m venv .venv
source .venv/bin/activate
cd simulator
pip install -e .

python3 bin/workflow.py experiments/yaml/industry_retreat/motivation.yaml 
```

Results land in `outputs/simulation/<scenario>_<timestamp>/` (CSV + plots). Override with `-o <path>`. See [`simulator/README.md`](simulator/README.md) for sweep configs, multi-client mode, and AIMD/Arolla retry-budget YAML.

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

## Postmortem Analysis

Reproductions of real outages, used to test whether different retry-control policies would have helped contain them. Two harnesses:

### Simulator scenarios

Four postmortem-informed scenarios in `simulator/experiments/yaml/post_mortem_simulation/`:

- `s01__buggy_release.yaml`
- `s02__shaky_foundations.yaml`
- `s03__thundering_herd.yaml`
- `s04__internal_retry_rogue.yaml`

Run any of them via the standard simulator workflow:

```bash
cd simulator
python bin/workflow.py experiments/yaml/post_mortem_simulation/s03__thundering_herd.yaml
```

### Live incident — Slack 2022-02-22 (cache stampede)

A self-contained Kind-based reproduction of the Slack outage on 2022-02-22 (Consul rolling restart → flushed memcached → DB overload → retry-driven cascading failure). Runs locally; no Emulab needed. See [`incident-slack-2022-02/README.md`](incident-slack-2022-02/README.md) for the full incident breakdown and step-by-step setup.

```bash
cd incident-slack-2022-02
./prepare-everything.sh         # bring up the Kind cluster + apps
python run_experiment.py        # drive load and collect metrics
```

## Notes

- The prototype `online-boutique` app disables the stock in-cluster loadgenerator by default in this repo and uses external controllable clients instead.
- External client profiles are "AWS-SDK-style" retry behaviors (not literal AWS SDK calls), because `online-boutique` is a generic HTTP application.