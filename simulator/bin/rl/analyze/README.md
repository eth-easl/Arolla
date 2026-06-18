# `bin/rl/analyze/` — policy & signal analysis

These scripts open a trained policy and ask *what is it actually using?* — which
telemetry signals drive its decisions. They are diagnostic tools, not part of the
train/eval/benchmark loop. Run from the `simulator/` directory.

## Scripts

| Script | Answers |
|--------|---------|
| `analyze_istio_retry_budget_signals.py` | Which observation signals DIRB (the final/report Istio policy) relies on, using learned weights + inference-time importance. Produces the `SIGNAL_ANALYSIS.md` shipped with `RB-RL.v5`. |
| `analyze_policy_first_layer.py` | First-layer weight magnitude per input feature (a quick learned-importance proxy). |
| `analyze_rollout_correlation.py` | Correlation / mutual information between observed signals and the actions taken during rollouts. |
| `plot_istio_focused_benchmark.py` | Focused plots from a benchmark output directory. |
| `plot_top_signal_action_correlations.py` | Plots the strongest signal-to-action correlations. |

## Output

Analysis artifacts (Markdown summaries, plots) are written next to the model or
to a path you pass on the CLI. As with eval/benchmark, anything generated for the
committed model is kept out of `models/` — pass an explicit output path if in
doubt.

## Example

```bash
python bin/rl/analyze/analyze_istio_retry_budget_signals.py \
  --model models/RB-RL.v5/model
```
