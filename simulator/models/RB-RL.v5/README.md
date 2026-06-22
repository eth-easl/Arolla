# RB-RL.v5 — DIRB final/report Istio retry-budget controller

This is the final trained PPO controller used for the report's RL results. In the
report it is named **DIRB**, short for **Dynamic Istio Retry Budget**. Its model
type is `IstioRetryBudgetMetastableEnv`: a server-side Istio retry-budget
controller over `percent` and `minRetryConcurrency`. It is committed so the
simulator's retry-budget RL can be evaluated and benchmarked out of the box,
without retraining. For the full guide see [`../../docs/rl/README.md`](../../docs/rl/README.md).

## What this model does

The agent observes caller-side, per-attempt mesh metrics and, every decision interval,
sets two Istio/Envoy retry-budget fields on the server:

- `retryBudget.percent`
- `retryBudget.minRetryConcurrency`

It is trained to keep goodput high and recover quickly from metastable retry
amplification after a fault clears, while avoiding retry storms and configuration churn.

## Specification

| Property | Value |
|----------|-------|
| Env class | `IstioRetryBudgetMetastableEnv` (`simulator.rl.istio_retry_budget_env`) |
| Algorithm | PPO (`MlpPolicy`, Stable-Baselines3) |
| Observation | `Box(18,)` caller-side, per-attempt features |
| Action | `MultiDiscrete([6, 6])` = 36 joint settings (absolute selection) |
| `percent` grid | `[0, 5, 10, 20, 35, 50]` |
| `minRetryConcurrency` grid | `[0, 1, 2, 3, 5, 8]` |
| Static baseline | `(20%, 3)` → action index `[3, 3]` |
| Decision interval | `5.0 s` |
| Observation window | `10.0 s` |
| Delta window | `5.0 s` |
| Scenario profile | `metastable_fairness` |
| Training template | `experiments/yaml/rl/istio_retry_budget_metastable.yaml` |

These three window values (`5 / 10 / 5`) are **part of the model's contract**. Use the same
values at evaluation, benchmarking, and live inference. See `training_spec.txt` and
`model_variant.txt` for the machine-readable spec. The full action/observation reference is in
[`../../docs/rl/action_observation_spaces.md`](../../docs/rl/action_observation_spaces.md).

## Files

| File | Purpose |
|------|---------|
| `model.zip` | PPO policy weights (load with `PPO.load`) |
| `vecnormalize_stats.pkl` | `VecNormalize` observation stats; **must** be loaded with the model |
| `training_spec.txt` | Human-readable training spec |
| `model_variant.txt` | Machine-readable spec (env class, knobs, windows) |
| `results/benchmark_summary.csv` | Per-scenario benchmark numbers vs no-budget and static budget |
| `results/benchmark_per_client.csv` | Per-client breakdown of the same benchmark |
| `results/SIGNAL_ANALYSIS.md` | Which telemetry signals the policy relies on |

`vecnormalize_stats.pkl` is loaded automatically from the directory next to `model.zip`,
so always pass the model path **without** the `.zip` suffix (e.g. `models/RB-RL.v5/model`).

## Inference / evaluation

Run from the `simulator/` directory:

```bash
python bin/rl/eval/eval_istio_retry_budget_metastable.py \
  --model models/RB-RL.v5/model \
  --yaml experiments/yaml/rl/istio_retry_budget_metastable.yaml \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5
```

## Benchmark vs baselines

```bash
python bin/rl/benchmark/run_istio_retry_budget_benchmarks.py \
  --model models/RB-RL.v5/model \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5
```

## Reproduce this model

```bash
python bin/rl/train/train_istio_retry_budget_metastable.py \
  --timesteps 1000000 \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5
```

Training writes a timestamped run under `trained_models/istio_retry_budget_metastable/`
(git-ignored). Copy the resulting `model.zip` and `vecnormalize_stats.pkl` here to promote a
new reference model. Because the action grid and observation semantics are fixed by the env,
older checkpoints are **not** compatible — retrain rather than reuse old normalization stats.
