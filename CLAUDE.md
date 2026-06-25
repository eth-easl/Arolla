# Project: Global Retry Budget Simulator

Research project studying retry amplification and global retry budgets in microservice architectures. Combines a discrete-event simulator with live Kubernetes/Istio prototype experiments.

## Repository Layout

- `simulator/` — Python discrete-event simulator (M/G/c/K queues, retry policies, AIMD budgets)
- `prototype/` — Kubernetes/Istio deployment scripts and manifests (Emulab infrastructure)
- `outputs/` — Experiment results (CSV metrics, plots)

## Development Setup

- Python 3.10+, virtual env at `.venv/`
- Install: `cd simulator && pip install -e .`
- Run tests: `cd simulator && python -m pytest tests/ -v`
- Formatters: `black`, linter: `ruff` / `flake8`, type checker: `mypy`

## Running Experiments

- Single experiment: `python simulator/bin/workflow.py simulator/experiments/yaml/<config>.yaml`
- Parameter sweep: `python simulator/bin/run_sweep.py simulator/experiments/yaml/<config>.yaml`
- Batch run: `python simulator/bin/run_batch.py simulator/experiments/yaml/<dir>/`
- Prototype experiments: `prototype/experiments/paper.sh` (dispatcher), `run_sweep.sh`, `run_grid.sh`

## Experiment Configs

Experiments are defined in YAML under `simulator/experiments/yaml/`. Configs specify:
- Service topology (latency distributions, workers, queue capacity)
- Retry policies (fixed, exponential, jittered backoff)
- Load limiters (token bucket, rate limiting)
- Retry budgets (static token bucket or AIMD self-tuning)
- Fault injection (partial failures, latency spikes)
- Workload (Poisson arrivals, load spikes)

## Code Conventions

- Config schemas use Pydantic v2 (`simulator/src/simulator/config/schema.py`)
- Policies are composable via middleware chain (`simulator/src/simulator/middleware/`)
- Commit messages follow conventional commits: `feat()`, `fix()`, `chore()`, `refactor()`
- This is a research prototype — prioritize correctness and clarity over production polish

# Prototype Setup (Emulab)

Before running any prototype experiment, verify `prototype/k8s-config.sh` has the correct node hostnames and SSH user for the current allocation. The file must be updated manually whenever a new Emulab experiment is started.

If the cluster is freshly allocated, bootstrap in order:

```bash
cd prototype
./deploy-k8s.sh                        # install K8s on all nodes (~10-15 min)
./deploy-istio.sh                      # install Istio + Gateway API CRDs
./deploy-app.sh online-boutique        # deploy Online Boutique
./deploy-cluster-profile.sh 2-replica  # deploy 2 replicas of cartservice
```

Verify the cluster is healthy before running experiments:

```bash
ssh <SSH_USER>@<MASTER_HOST> "kubectl get nodes && kubectl -n online-boutique get pods"
```

All nodes should be `Ready` and all pods `Running` (~11 pods). Once healthy, run a sanity-check scenario:

```bash
prototype/experiments/run-experiment.sh \
    --name <name> \
    --scenario sustained-failure \
    --policies envoy-retry-budget \
    --warmup 30 --prefault 15 --fault 30 --recovery 30 --cooldown 15
```

### Warm-up sweeps before measurement runs

A freshly-deployed cluster needs to be "warmed in" before the metrics it produces are comparable to prior runs. On a cold cluster, image pulls, sidecar JIT/connection-pool warm-up, kernel page cache, and TCP-conntrack state are all empty, which causes brief 5xx bursts during the first 1–2 s of each policy's pre-fault phase. Rate-based outlier detection (the `circuit-breaker` policy) is especially sensitive to this and can lock into a degraded baseline on run 1 even though the steady-state cluster is fine.

**Always run two throwaway sweeps on a new cluster before the measurement runs you actually plan to keep.** Empirically run 3+ on the same cluster matches historical baselines, while runs 1–2 can show 3–5× lower pre-fault goodput on `circuit-breaker`. Use the cheap `post-cart-stress-open` sweep:

```bash
# Throwaway warm-up runs (results discarded)
NUM_LOADERS=4 prototype/experiments/run-experiment.sh \
    --policies no-control,circuit-breaker,envoy-retry-budget,arolla \
    --client-profiles post-cart-stress-open \
    -F cartservice-100pct \
    --warmup 30 --prefault 60 --fault 10 --recovery 60 --cooldown 10
# repeat once more, then start the real measurements
```

### Arolla policy: start the wasm HTTP server first

The `arolla` policy uses a `WasmPlugin` with `failStrategy: FAIL_CLOSE` that fetches `arolla_filter.wasm` from `http://__MASTER_IP__:8000/`. If no HTTP server is serving the binary on the master, every request through an arolla-enabled sidecar is rejected (you'll see `arolla` runs with `goodput=0` and `arolla_admitted=arolla_rejected=0`).

Before any sweep that includes `arolla`, push the prebuilt wasm to the master and start a Python HTTP server (build instructions in `prototype/arolla-filter/README.md`):

```bash
source prototype/k8s-config.sh
ssh ${SSH_USER}@${MASTER_HOST} 'mkdir -p ~/arolla-wasm'
scp prototype/arolla-filter/target/wasm32-wasip1/release/arolla_filter.wasm \
    ${SSH_USER}@${MASTER_HOST}:~/arolla-wasm/
ssh ${SSH_USER}@${MASTER_HOST} \
    'pkill -f "http.server 8000" 2>/dev/null; cd ~/arolla-wasm && \
     setsid nohup python3 -m http.server 8000 --bind 0.0.0.0 \
     </dev/null >~/arolla-wasm/server.log 2>&1 & disown'

# Verify reachable on the private fabric
ssh ${SSH_USER}@${MASTER_HOST} \
    "curl -sS -o /dev/null -w '%{http_code} %{size_download} bytes\n' \
     http://${MASTER_IP}:8000/arolla_filter.wasm"
```

The server must be restarted any time the master node reboots. If you don't intend to run `arolla`, you can skip this entirely — the other three policies don't depend on it.

## Kubeconfig Isolation

The Emulab cluster kubeconfig lives at `~/.kube/config-emulab` and is **never merged into `~/.kube/config`**. The context is named `emulab` (not the default `kubernetes-admin@kubernetes`) to prevent collisions with other clusters on the same machine.

All prototype scripts source `prototype/k8s-config.sh`, which sets `export KUBECONFIG=~/.kube/config-emulab`. For ad-hoc `kubectl` commands outside of those scripts, do the same:

```bash
source prototype/k8s-config.sh
kubectl get nodes   # hits Emulab, not any other local cluster
```

Never run `kubectl config use-context` or `kubectl config merge` without explicitly passing `--kubeconfig ~/.kube/config-emulab`, or you risk cross-contaminating the configs.

## Important Context

- This is active research: multiple experiment branches exist (dev/*, prototype/*, simulator/*)
- The simulator is cross-checked against real Kubernetes/Istio deployments on Emulab
- Do NOT delete or overwrite files in `outputs/` without asking — these may contain hard-to-reproduce experimental results
- When modifying simulator core (engine, runtime, policies), always run the test suite afterward
