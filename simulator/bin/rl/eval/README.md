# `bin/rl/eval/` — evaluation scripts

These scripts load a saved policy and run it on **one scenario**, comparing it to
the No-Budget and Static-Budget baselines, and produce a comparison plot plus
metrics. Run from the `simulator/` directory.

Use `eval/` when you want to inspect one scenario in detail. Use `benchmark/`
when you want aggregate CSVs across a scenario suite.

Typical reasons to use `eval/`:

- sanity-check that a model and `vecnormalize_stats.pkl` load correctly;
- visualize the agent's actions during one partial failure or load spike;
- debug a surprising benchmark result by rerunning that scenario alone.

## Scripts

| Script | Evaluates | Notes |
|--------|-----------|-------|
| `eval_istio_retry_budget_metastable.py` | DIRB / Istio retry-budget model (final/report) | The one to use for `RB-RL.v5`; report name: Dynamic Istio Retry Budget |
| `eval_rl_scenario.py` | Token bucket, single client | Also the shared evaluation engine imported by the others |
| `eval_rl_scenario_relative.py` | Token bucket, relative actions | |
| `eval_rl_metastable_absolute.py` | Token bucket, multi-client | |
| `eval_rl_metastable_relative.py` | Multi-client, relative actions | |
| `eval_rl_metastable_hybrid.py` | Hybrid 3-knob | Variant auto-detected from `model_variant.txt` |

## Inference contract

- Pass `--model <dir>/model` **without** `.zip`. The matching
  `vecnormalize_stats.pkl` is loaded automatically from the same directory.
- Use the **same** `--decision-interval-s / --observation-window-s /
  --delta-window-s` the model was trained with (for `RB-RL.v5`: `5 / 10 / 5`).

## Output

The comparison plot defaults to a clean, git-ignored location:

- model under `trained_models/...` → next to the model;
- committed model under `models/...` → `outputs/rl/istio_retry_budget_eval/<model_name>/`.

Override with `--output <path.png>`.

## Example

```bash
python bin/rl/eval/eval_istio_retry_budget_metastable.py \
  --model models/RB-RL.v5/model \
  --yaml experiments/yaml/rl/istio_retry_budget_metastable.yaml \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5
```
