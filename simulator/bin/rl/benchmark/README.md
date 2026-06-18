# `bin/rl/benchmark/` — benchmark suites

These scripts run a saved policy across a **set** of fault/load scenarios and
compare it to baselines (No Budget, Static Budget, and sometimes a Best-Static
search). They write two aggregate CSVs plus per-scenario plots/JSON. Run from the
`simulator/` directory.

## What "benchmarking" means here

A benchmark is not training. It is a repeatable test harness:

1. Load a trained model (`--model <run>/model`, without `.zip`).
2. Run the same scenario with several policies.
3. Compare success, retry amplification, latency, recovery, and per-client
   fairness.
4. Save aggregate CSVs and per-scenario artifacts so model versions can be
   compared later.

The important policies are:

| Policy | Meaning | Why it is useful |
|--------|---------|------------------|
| `No Budget` | Retries are allowed by clients, but there is no server-side retry budget limiting them. | Shows retry amplification risk when retries are unconstrained. |
| `Static Budget` | The YAML budget is held fixed for the whole run, e.g. Istio `(20%, 3)`. | Represents a hand-configured production setting. |
| `Best Static` | A grid search over static settings, when supported by the runner. | Shows the best fixed setting in hindsight; useful but not deployable as an online policy. |
| `RL Agent` | The PPO policy changes the budget online every decision interval. | Tests whether adaptive control recovers better than one fixed setting. |

The point of the benchmark suite is not just "does RL win one plot?". It asks:
does the policy behave well across different kinds of stress, and does it recover
after the fault clears without causing retry storms?

## Scripts

| Script | Model family | Scenario set |
|--------|--------------|--------------|
| `run_istio_retry_budget_benchmarks.py` | DIRB / Istio retry budget (final/report) | `experiments/yaml/rl/istio_retry_budget_benchmarks/` |
| `run_benchmarks.py` | Token bucket, single client | `experiments/yaml/rl/benchmarks/` |
| `run_benchmarks_relative.py` | Token bucket, relative actions | `experiments/yaml/rl/benchmarks/` |
| `run_metastable_benchmarks_absolute.py` | Token bucket, multi-client | metastable suite |
| `run_metastable_benchmarks_relative.py` | Multi-client, relative actions | metastable suite |
| `run_metastable_benchmarks_hybrid.py` | Hybrid 3-knob | metastable suite |

Helpers (not run directly):

- `metastable_benchmark_suite.py` — resolves the metastable benchmark YAML set.
- `run_static_metastable_failure.py` — the static / best-static baseline search.
- `compare_rl_vs_baseline.py` — quick single-scenario RL-vs-baseline comparison.

## Scenario families

The YAML names are intentionally descriptive. These are the fault/load families
you will see in the benchmark directories:

| Family | What happens in the simulator | What it tests |
|--------|-------------------------------|---------------|
| `partial_failure` | For a time window, the service randomly fails some attempts (`p_fail`). Clients retry those failures. | Whether the budget prevents retries from amplifying a transient server fault. |
| `load_spike` | Client request rates multiply for a time window (`rps_multiplier`). | Whether the policy can protect the queue during overload and recover once demand falls. |
| `compound_failure` | A partial failure overlaps with a later load spike. | Whether the policy can handle interacting stressors instead of one clean incident. |
| `recovery_with_spike` | A failure/recovery period is followed by another spike. | Whether the policy reopens carefully after recovery instead of staying too strict or too loose. |
| `switchback_adversarial` | Two phases prefer opposite budget settings: first a severe fault where tight retry control helps, then a heavy spike where a looser setting may preserve useful retries. | Whether adaptive control can beat any single static setting across non-stationary conditions. |
| `fairness` / `metastable_fairness` | Multiple clients (`gold`, `silver`, `bronze`) share one service and one retry budget. | Whether the policy preserves the worst-off client's success, not just aggregate success. |
| `static_struggle` | A scenario tuned so one static budget setting performs poorly in at least one phase. | Stress-tests the "one fixed setting is enough" assumption. |

`switchback_adversarial` is the least obvious name. It means the environment
"switches back" between regimes: the controller should clamp down during the
early fault, then adapt when the later traffic spike changes what an optimal
budget looks like. A static budget cannot know this timing; it must compromise.

## Output

Each suite writes, under its output directory:

```text
<out_dir>/
  benchmark_summary.csv       # one row per (scenario, policy) with headline metrics
  benchmark_per_client.csv    # per-client breakdown
  <scenario>/                 # per-scenario plots + JSON details
```

Important columns:

| Column / suffix | Meaning | Read it as |
|-----------------|---------|------------|
| `sr_fault_agg` | Aggregate success rate during detected fault windows. | Higher is better. |
| `load_amp` | Load amplification: total attempts divided by original requests. | Lower means fewer retry storms. |
| `retry_eff` | Retry efficiency: how often retries help rather than waste work. | Higher is better, but only if load amplification stays controlled. |
| `avg_recovery` | Average post-fault recovery quality. | Higher means the system returns to useful throughput faster. |
| `p50`, `p95`, `p99` | Latency percentiles. | Lower is better, especially `p95`/`p99` under overload. |
| `*_sr_fault` in `benchmark_per_client.csv` | Per-client success during fault windows. | Shows whether one client tier was sacrificed. |
| `*_retry_share` in `benchmark_per_client.csv` | Share of retries used by each client. | Helps diagnose fairness and retry dominance. |

Default `<out_dir>` (override with `--output-dir`):

- model under `trained_models/...` → next to the model (git-ignored run dir);
- committed model under `models/...` → `outputs/rl/<suite>/<model_name>/`
  (git-ignored), so the reference model folder stays clean.

The committed headline numbers for the shipped model live in
[`../../models/RB-RL.v5/results/`](../../models/RB-RL.v5/results); these scripts
regenerate them for any model.

## Example

```bash
python bin/rl/benchmark/run_istio_retry_budget_benchmarks.py \
  --model models/RB-RL.v5/model \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5
```
