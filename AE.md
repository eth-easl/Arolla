# Arolla: NSDI '27 Artifact Evaluation

This document explains how to provision the testbed and reproduce
the paper's prototype results.

## 1. Overview

The repository contains three artifact components, `prototype/` is relevant to the paper artifact evalution.

- `prototype/`: the evaluated Kubernetes/Istio prototype, two Rust-to-Wasm Arolla filters, workloads, fault manifests, and paper experiment harness.
- `simulator/`: the discrete-event simulator for studing retry admission policies.
- `Postmortem-anslysis/`

The artifact reproduces the following results from the paper:

| Figures | Description |
|---|---|
| Figure 4 | Effectiveness under a sustained failure.  |
| Figure 5 | Recovery time across three axes of failure severity.  |
| Figure 6 | Fairness of retry admission across heterogeneous clients. |



## 2. Setup

You will need a [CloudLab](https://www.cloudlab.us/) account and to be part of a "project" to start cloudlab experiments.  if you don't already have an account. If you don't have an account and project you can join, you can create a new project by following the instructions in the CloudLab guide ("2.1.2 Create a new project"): the applications for new projects are reviewd by CloudLab staff, which may take a few days.


### 2.1 Cloudlab setup

1. Choose Start Experiment under the Experiments tab
2. Use the dafault proflle (small-lan) and click Next
3. Parameterize
  - Number of nodes: 6
  - Select OS image: Ubuntu 24.04
  - Optional phsical node type: emulab - d430
4. Finalize the setup with experiment name as you prefer
5. After you create the experiment, you might want to extend it the duration of the experiment to a few days, so that you have enough time to run the experiments and reproduce the results.

Make sure to configure ssh access to the nodes. 
```bash
eval "$(ssh-agent -s)"
ssh-add <your cloudlab private key>
```

### 2.2 Local setup
Clone the experiment repository and install the required dependencies. You can run this on your local machine or you can use another cloudlab node (you should start a new experiment for this, DON'T run it on your 6-node cluster).
```bash
git clone https://github.com/yazhuo/globalRetryBudget.git
cd globalRetryBudget
```

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e './simulator[dev]' requests
```

Install Rust and the Wasm target If you already have Rust installed, you can skip the first command.

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env
rustup target add wasm32-wasip1
```

#### 2.2.1 Configure the allocated nodes

All commands below run on the evaluator's local machine. 

Edit these fields in `prototype/k8s-config.sh`:

- `SSH_USER` (your cloudlab username)
- `MASTER_HOST` (such as: "pcxxx.emulab.net")
- `WORKER_HOSTS` (such as: "pcxxx.emulab.net" "pcxxx.emulab.net" "pcxxx.emulab.net" "pcxxx.emulab.net")
- `CLIENT_HOST` (such as: "pcxxx.emulab.net")


| Role | Count | `prototype/k8s-config.sh` setting |
|---|---:|---|
| Kubernetes control plane | 1 | `MASTER_HOST` |
| Kubernetes workers | 4 | `WORKER_HOSTS` |
| External load generator | 1 | `CLIENT_HOST` |

Verify SSH before provisioning:

```bash
source prototype/k8s-config.sh
ssh "${SSH_USER}@${MASTER_HOST}" hostname
for host in "${WORKER_HOSTS[@]}" "${CLIENT_HOST}"; do
  ssh "${SSH_USER}@${host}" hostname
done
```

#### 2.2.2 Provision Kubernetes and Istio


```bash
REPO=$(git rev-parse --show-toplevel)
cd "$REPO/prototype"
./deploy-k8s.sh
./deploy-istio.sh
```
This part takes approximately 15 minutes. After it's finished, confirm that the cluster is reachable before deploying the application:

```bash
kubectl get nodes
```

All five Kubernetes nodes should report `Ready`.

#### 2.2.3 Deploy Online Boutique and Arolla

Deploy the application and apply the resource profile used by the paper:

```bash
./deploy-app.sh online-boutique
./deploy-cluster-profile.sh 2-replica
```

Build and deploy Arolla:

```bash
./deploy-policy.sh build-wasm
./deploy-policy.sh upload-wasm
./deploy-policy.sh serve-wasm
./deploy-policy.sh apply arolla
```

Verify the deployment

```bash
./deploy-policy.sh status
```

#### 2.2.4 [Optional] end-to-end smoke test


```bash
cd "$REPO/prototype/experiments"

OUTPUT_BASE="$REPO/outputs/ae-smoke" NUM_LOADERS=1 \
  ./run-experiment.sh \
  --policies arolla \
  --client-profiles post-cart-stress-open \
  -F cartservice-100pct \
  --warmup 10 --prefault 15 --fault 5 --recovery 15 --cooldown 5
```

A successful run creates one timestamped directory under
`outputs/ae-smoke/post-cart-stress-open/[datetime]`, including `experiment.json`,
`arolla/timeline.json`, client CSV files, `summary.csv`, and diagnostic plots.
This is just for functionality check.

## Reproduce experiments

### Figure 4: effectiveness under a sustained failure

This experiment compares `no-control`, `circuit-breaker`, `envoy-retry-budget`, and `arolla` at 1,200 offered requests/s. 
It takes about 20-25 minutes including policy switches, restarts, collection, and plotting.

```bash
cd "$REPO/prototype/experiments"

./paper.sh effectiveness_experiment

RUN_DIR=$(ls -td "$REPO"/outputs/nsdi/post-cart-stress-open/*/ | head -1)
./paper.sh plot_effectiveness "$RUN_DIR"
```

Expected paper outputs in `$RUN_DIR/paper_figs/`:

| Figure | File |
|---|---|
| Figure 4a: client-observed success rate | `success-rate.pdf` |
| Figure 4b: frontend p50 latency | `latency-ts-p50-linear.pdf` |
| Figure 4c: retries received by service | `retries-received-log.pdf` |


### Figure 5: recovery sensitivity

This experiment sweeps three axes of failure severity: offered load, failure rate, and fault duration. Each sweep takes about 3-6 hours. The paper aggregates 10 independent runs per point, each run generates a timestamped directory under `outputs/nsdi/[failure_rate|failure_duration|rps]_sweep`. The paper plots are generated from the aggregated data.

For the sake of time, you can run a single sweep per axis to verify the experiment. Feel free to run the full sweep if you have time.

```bash
cd "$REPO/prototype/experiments"
./paper.sh recovery_vs_load
./paper.sh recovery_vs_failure_rate
./paper.sh recovery_vs_fault_duration
```

Generate the plots:

```bash
REPO=$(git rev-parse --show-toplevel)

./paper.sh plot_recovery_vs_load_multirun "$REPO/outputs/nsdi/rps_sweep"
./paper.sh plot_recovery_vs_failure_rate_multirun "$REPO/outputs/nsdi/failure_rate_sweep"
./paper.sh plot_recovery_vs_fault_duration_multirun "$REPO/outputs/nsdi/failure_duration_sweep"
```

Expected outputs:

| Figure | File |
|---|---|
| Figure 5a | `rps_sweep/recovery-vs-load-multirun.pdf` |
| Figure 5b | `failure_rate_sweep/recovery-vs-failure-rate-multirun.pdf` |
| Figure 5c | `failure_duration_sweep/recovery-vs-fault-duration-multirun.pdf` |


### Figure 6: Fairness across heterogeneous clients.


This part takes about 20 minutes.

```bash
REPO=$(git rev-parse --show-toplevel)
cd "$REPO/prototype/experiments"
./paper.sh fairness_experiment_same_rps
./paper.sh fairness_experiment_diff_rps

SAME_DIR=$(ls -td "$REPO"/outputs/nsdi/fairness-same-rps/*/ | head -1)
DIFF_DIR=$(ls -td "$REPO"/outputs/nsdi/fairness-diff-rps/*/ | head -1)

./paper.sh plot_fairness "$SAME_DIR" "$DIFF_DIR"
```

Expected outputs:

| Figure | File |
|---|---|
| Figure 6a | `outputs/nsdi/fairness-same-rps-count.pdf` |
| Figure 6b | `outputs/nsdi/fairness-diff-rps-count.pdf` |