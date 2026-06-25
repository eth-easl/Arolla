# Prototype experiments

End-to-end orchestrator for running retry-policy experiments on the
Online Boutique prototype. Supports both single-run comparisons and a
30-scenario Phase-B sweep that benchmarks the RL-boosted retry-budget
policy (`rb-rl-v5`) against three baselines.

---

## Files

```
prototype/experiments/
│
├── run-experiment.sh        ← single-run orchestrator (all policy phases)
├── run_full_sweep.sh        ← 30-scenario batch driver (Phase-B sweep)
├── run_rl.sh                ← dev/debug: one RL run + comparison plots
│
├── scenarios/               ← .conf files for named single-run scenarios
│   ├── sustained-failure.conf   (paper §6.2.1 — 50% abort for 60 s)
│   ├── recovery-overload.conf   (paper §6.2.2 — product-catalog fault)
│   └── transient.conf           (single burst approximation)
│
├── sweep_scenarios.csv      ← canonical 25 S-series scenarios (Phase-B)
├── sweep_scenarios_ext.csv  ← 5 extended scenarios: MS1–MS3, AS1–AS2
│
├── rl/                      ← RL controller implementation
│   ├── rl_controller.py         ← RL agent (policy gradient, in-process)
│   ├── rl_obs_envoy.py          ← Envoy /stats scraper for observations
│   ├── rl_obs_schema.py         ← observation field schema
│   ├── rl_configs/v{1..5}/      ← versioned training configs + model zips
│   ├── rl_controller_image/     ← Dockerfile + build/distribute scripts
│   ├── ensure_rl_configmaps.sh  ← idempotent RL ConfigMap setup
│   └── envoy_stats_sample.prom  ← test fixture for rl_obs_envoy
│
├── analyze.py               ← per-run metrics aggregator + plotter
├── classify_runs.py         ← label outcomes (Recovered / Metastable / …)
├── sustained_recovery.py    ← "sustained recovery" metric implementation
├── plot_rl_comparison.py    ← multi-policy RL comparison figures
│
├── bench_decision_diff.py   ← offline comparison of two RL decision traces
├── bench_summarize.py       ← summarise bench_decision_diff output
├── measure_overhead.sh      ← CPU/memory overhead measurement sweep
├── resource_sampler.py      ← in-experiment resource sampler (kubectl top)
│
├── paper.sh                 ← named-experiment dispatcher (effectiveness, fairness)
├── paper_plotting.py        ← paper-ready figures for single runs + sweeps
├── run_sweep.sh             ← single-parameter sweep driver (YAML-configured)
├── run_grid.sh              ← cartesian-grid sweep driver
├── sweeps/*.yaml            ← sweep/grid configs (rps, failure-rate, sensitivity, …)
├── plot_sensitivity.py      ← sweep recovery-vs-parameter plots
├── plot_param_sensitivity.py, plot_grid_sensitivity.py
├── plot_arolla_sensitivity{,_multirun}.py
├── plot_rb_sensitivity{,_multirun}.py
├── plot_failure_sweep.py, plot_failure_duration_multirun.py
├── plot_fairness_two_panel.py, plot_slack_postmortem.py
├── verify_retry_budget.sh   ← assert envoy-retry-budget is live in Envoy
├── verify_circuit_breaker.sh← assert circuit-breaker is live in Envoy
│
└── tests/
    ├── test_sustained_recovery.py
    └── test_classify_decision.py
```

---

## Quick start

### Single-run comparison (all four policies)

    prototype/experiments/run-experiment.sh

Defaults match the paper §6.2.1 scenario: 60 s warmup, 30 s pre-fault,
60 s fault, 60 s recovery, 30 s cooldown = ~16 minutes for four policies.

```bash
# Dry-run: print schedule and exit
prototype/experiments/run-experiment.sh --dry-run

# One policy only
prototype/experiments/run-experiment.sh --policies arolla

# Override scenario and fault duration
prototype/experiments/run-experiment.sh --scenario recovery-overload --fault 90
```

### 30-scenario Phase-B sweep

Run all 30 scenarios (25 canonical + 5 extended) against
`no-control`, `envoy-retry-budget`, `arolla`, and `rb-rl-v5`:

```bash
prototype/experiments/run_full_sweep.sh
```

Output lands in `outputs/prototype/<sweep_ts>/run1/<scenario_label>/<policy>/`.

Subset options:
```bash
# Only canonical scenarios, two policies
prototype/experiments/run_full_sweep.sh \
  --ids S01,S02,S05 \
  --policies no-control,rb-rl-v5

# Three repeats
prototype/experiments/run_full_sweep.sh --repeats 3
```

### RL-only dev run

```bash
# Single scenario + RL controller + comparison plots
prototype/experiments/run_rl.sh \
  --config rl/rl_configs/v5/rb-rl-v5.yaml \
  --scenario sustained-failure
```

### Analyze a completed run

```bash
prototype/experiments/analyze.py outputs/prototype/runs/20260406_200000
```

Regenerates `summary.csv` and `plots/*.pdf` in place.

---

## Policy directory layout

Every experiment produces one subdirectory per policy:

```
<run_dir>/
├── experiment.json          ← phase timestamps + config snapshot
├── summary.csv              ← one row per policy: goodput, recovery, …
├── plots/
│   ├── goodput.pdf
│   ├── amplification.pdf
│   ├── retry-efficiency.pdf
│   └── recovery-time.pdf
├── no-control/
│   ├── timeline.json
│   ├── client-metrics/*.csv
│   ├── sidecar-stats/*.stats
│   └── traffic_gen.log
├── circuit-breaker/
├── envoy-retry-budget/
├── arolla/
└── rb-rl-v5/                ← RL variant (run_full_sweep.sh) or
                             ← envoy-retry-budget/ (run_rl.sh dev runs)
                                 └── rl-controller/
                                         ├── rl-decisions.csv
                                         └── resource-usage.csv
```

`plot_rl_comparison.py` discovers both `rb-rl-v5/` and `envoy-retry-budget/`
automatically.

---

## Scenario CSVs

**`sweep_scenarios.csv`** — 25 canonical scenarios (S01–S25).

Columns: `id, rate_rps, fault_duration, fault_rate, default_label`

All canonical scenarios fault `cartservice` with `post-cart-stress-open` load.
`fault_rate` is the abort percentage; `fault_manifest` is
`cartservice-<fault_rate>pct.yaml`.

**`sweep_scenarios_ext.csv`** — 5 extended scenarios (MS1–MS3 multi-spike,
AS1–AS2 alternate-service).

Columns: `id, client_profile, fault_manifest, rate_rps, fault_duration,
fault_rate, num_spikes, inter_spike_gap, rl_callee, rl_caller_labels,
default_label`

`rl_callee` / `rl_caller_labels` retarget the RL observation to the faulted
service rather than the default `cartservice`.

---

## RL controller versions

| Version | Config | Notes |
|---------|--------|-------|
| v1 | `rl/rl_configs/v1/` | Initial policy-gradient prototype |
| v2–v4 | `rl/rl_configs/v2–4/` | Observation-space and reward tuning |
| v5 | `rl/rl_configs/v5/rb-rl-v5.yaml` | **Phase-B baseline** — normalized observations, 30-scenario sweep |

Build and distribute the v5 controller image:

```bash
cd prototype/experiments/rl/rl_controller_image
./build.sh v5
./distribute.sh v5    # pushes to all worker nodes
```

---

## Prerequisites

- `kubectl` context pointed at the Emulab cluster (see `CLAUDE.md`)
- Online Boutique deployed: `prototype/deploy-app.sh online-boutique`
- Arolla wasm built and served (see `prototype/arolla-filter/README.md`)
- RL controller image built and distributed for Phase-B sweeps (see above)
- `python3` with `pandas`, `numpy`, `matplotlib`, `scipy`

---

## Metrics in `summary.csv`

| Column                  | Meaning |
|-------------------------|---------|
| `amplification`         | total attempts / first attempts during fault |
| `retry_efficiency_pct`  | retries that succeeded / total retries × 100 |
| `avg_goodput_prefault`  | mean successful req/s during pre-fault |
| `avg_goodput_fault`     | mean successful req/s during fault |
| `recovery_sec`          | seconds to sustained goodput ≥ 95% pre-fault |
| `sustained_recovered`   | bool — met goodput + success-rate threshold for full window |
| `arolla_admitted`       | sum of `arolla_retries_admitted_total` across sidecars |
| `arolla_rejected`       | sum of `arolla_retries_rejected_total` across sidecars |

Recovery is measured with the **sustained-recovery** criterion
(`sustained_recovery.py`): goodput and success-rate must exceed their
respective thresholds continuously for a configurable window (default 5 s),
not just at a single crossing point.

---

## Known gaps

- **§6.2.2 recovery-overload** uses 50% abort + 30% delay on productcatalog,
  not the "fully offline" 100% outage in the paper. Add a new manifest for
  an exact match.
- **§6.2.3 chain-depth amplification** is not scripted — needs per-hop
  retry-rate queries against Istio Prometheus.
- **gRPC trailer handling** in the Arolla filter treats HTTP 200s as success;
  see `arolla-filter/README.md` "Known limitations".
