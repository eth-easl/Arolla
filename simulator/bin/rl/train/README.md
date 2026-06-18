# `bin/rl/train/` — training scripts

Each script trains a PPO policy for one model family (see
[`../../../docs/rl/models.md`](../../../docs/rl/models.md) for what each model is
and its reward). Run from the `simulator/` directory.

If you want to reproduce **DIRB**, the final model type from the report, use
`train_istio_retry_budget_metastable.py`. DIRB means **Dynamic Istio Retry
Budget**. This trainer produces checkpoints like `RB-RL.v5`: an Istio
retry-budget controller over `percent` and `minRetryConcurrency`.

## What training does

Training wraps a simulator YAML in a Gymnasium environment, runs many simulated
episodes, and updates a PPO neural-network policy. The agent does **not** change
the YAML itself. It changes only the runtime budget knobs exposed by the
environment, such as Istio `percent` / `minRetryConcurrency` or token-bucket
`refill_rate` / `bucket_capacity`.

Training is for creating a new checkpoint. If you already have a checkpoint and
just want to test it, use `../eval/` or `../benchmark/` instead.

## Training loop at a glance

![RL training loop](../../../docs/rl/assets/rl-training-loop.png)

The diagram shows one PPO training cycle:

- `reset()` loads a YAML scenario and starts a new simulated episode.
- `step(action)` applies the agent's retry-budget action to the simulator.
- The simulator advances by one decision interval, e.g. `5s` for `RB-RL.v5`.
- The environment builds the next observation `o_t` and reward `r_t` from recent
  caller-side metrics.
- PPO stores `(o_t, a_t, r_t)` samples and periodically updates the policy.
- After training, the frozen artifacts are `model.zip` and
  `vecnormalize_stats.pkl`; both are required for inference.

## Output

Every real training run creates a timestamped, **git-ignored** directory:

```text
trained_models/<family>/run_<timestamp>_<steps>/
  model.zip  vecnormalize_stats.pkl  best_model/  eval_logs/  tb_logs/  plots/
  training_spec.txt  model_variant.txt
```

`<family>` per script:

| Script | `<family>` directory | Model family |
|--------|----------------------|--------------|
| `train_istio_retry_budget_metastable.py` | `istio_retry_budget_metastable/` | DIRB, Dynamic Istio Retry Budget (**final report model type**) |
| `train_random.py` | `trained_models/` (root) | Token bucket, single client |
| `train_relative.py` | `relative_action/` | Token bucket, relative actions |
| `train_metastable_absolute.py` | `metastable_absolute_action/` | Token bucket, multi-client |
| `train_metastable_relative.py` | `metastable_relative_action/` | Multi-client, relative actions |
| `train_metastable_hybrid_absolute.py` | `metastable_hybrid_absolute_action/` | Hybrid 3-knob (absolute) |
| `train_metastable_hybrid_relative.py` | `metastable_hybrid_relative_action/` | Hybrid 3-knob (relative) |
| `train_rl_test.py`, `train_random_test.py` | `_smoke/` | Smoke tests (throwaway) |

## Common flags

Most trainers accept:

- `--timesteps N` — total PPO timesteps (default 500k).
- `--decision-interval-s`, `--observation-window-s`, `--delta-window-s` — the
  control/observation timing. **Record these**: inference must use the same.
- `--scenario-profile` — `metastable_fairness` (default) or `switchback_adversarial`.
- `--skip-training --run-dir <dir>` — re-evaluate an existing run without retraining.

## Examples

```bash
# DIRB final report model type (matches the shipped RB-RL.v5 timing)
python bin/rl/train/train_istio_retry_budget_metastable.py \
  --timesteps 1000000 --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5

# Legacy token-bucket metastable model
python bin/rl/train/train_metastable_absolute.py --timesteps 1000000

# 30-second sanity check that the training loop runs end to end
python bin/rl/train/train_rl_test.py
```

## Promoting a model

To ship a trained run for out-of-the-box inference, copy `model.zip` and
`vecnormalize_stats.pkl` (plus the spec files) from its run directory into
`simulator/models/<name>/`, and add a short model card there. `models/` is the
only model directory tracked by git.
