# Istio Retry-Budget RL Signal Analysis

This report analyzes the final `istio_retry_budget_metastable` PPO controller. It separates **learned importance** (what the policy network is sensitive to) from **inference importance** (which signals change actions when removed from real rollout states).

## Dataset

- Recorded decisions: 749
- Scenarios/seeds: 40
- Phase counts: {'pre_fault': 162, 'fault': 288, 'recovery': 154, 'post_recovery': 145}
- Fault-scope counts: {'randomized_metastable': 20, 'generalized_single_fault': 10, 'generalized_multi_fault': 10}
- Recovery classes: {'quick_recovery': 23, 'censored_or_not_recovered': 15, 'slow_recovery_metastable_like': 2}
- Observation space: 18 caller-side, per-attempt features.
- Action space: `retryBudget.percent` in `[0.0, 5.0, 10.0, 20.0, 35.0, 50.0]` and `minRetryConcurrency` in `[0, 1, 2, 3, 5, 8]`.

## Feature Groups

| index | feature | group | description |
| --- | --- | --- | --- |
| 0 | success_rate_agg | client health | Aggregate per-attempt success rate across all clients. |
| 1 | min_client_success | client health | Worst per-client attempt success rate in the observation window. |
| 2 | retry_ratio | retry cost | Fraction of attempts that are retries. |
| 3 | window_load_amplification | retry cost | Total attempts divided by first attempts; the retry amplification signal. |
| 4 | window_retry_efficiency | retry cost | Fraction of retry attempts that eventually succeeded. |
| 5 | retry_fairness_gap | retry cost | Mismatch between retry share and first-attempt share across clients. |
| 6 | p95_latency_pressure | overload | Attempt p95 latency divided by the attempt timeout. |
| 7 | budget_reject_rate | overload | Fraction of retry admissions rejected by the Istio retry budget. |
| 8 | server_fail_rate | overload | Fraction of attempts failing as server failure or queue full. |
| 9 | deadline_rate | overload | Fraction of attempts timing out or reaching the attempt timeout. |
| 10 | delta_success_agg | trend | Short-window change in aggregate success rate. |
| 11 | delta_window_load_amplification | trend | Short-window change in load amplification. |
| 12 | budget_utilization | budget state | Active retries divided by the current retry concurrency limit. |
| 13 | retry_pressure_vs_limit | budget state | Retry attempt rate divided by the current concurrency limit. |
| 14 | current_percent_norm | action memory | Current retryBudget.percent divided by 100. |
| 15 | current_min_retry_concurrency_norm | action memory | Current minRetryConcurrency divided by 10. |
| 16 | previous_percent_norm | action memory | Previous retryBudget.percent divided by 100. |
| 17 | previous_min_retry_concurrency_norm | action memory | Previous minRetryConcurrency divided by 10. |

## Top Learned Signals

These features have the strongest combined first-layer and gradient sensitivity in the PPO policy.

| feature | group | rank_score | first_layer_abs_weight_share | selected_action_gradient_share |
| --- | --- | --- | --- | --- |
| delta_window_load_amplification | trend | 0.1244 | 0.08152 | 0.1392 |
| current_min_retry_concurrency_norm | action memory | 0.1071 | 0.06939 | 0.1168 |
| window_load_amplification | retry cost | 0.09865 | 0.06709 | 0.1131 |
| previous_percent_norm | action memory | 0.06982 | 0.05554 | 0.07886 |
| current_percent_norm | action memory | 0.06521 | 0.05822 | 0.0657 |
| window_retry_efficiency | retry cost | 0.06109 | 0.05623 | 0.06076 |
| budget_reject_rate | overload | 0.0573 | 0.05126 | 0.0595 |
| delta_success_agg | trend | 0.05259 | 0.0548 | 0.05332 |

## Telemetry vs Controller Memory

The controller can rely on two kinds of signals: external telemetry that says something about the service state, and action-memory features that tell it what budget it already applied. For explaining metastable failures to lab members, the telemetry ranking is usually the more useful one; action memory mostly explains action stability and hysteresis.

**Top external telemetry signals**

| feature | group | combined_rank_score | learned_rank_score | inference_rank_score |
| --- | --- | --- | --- | --- |
| window_load_amplification | retry cost | 0.2404 | 0.09865 | 0.3821 |
| delta_window_load_amplification | trend | 0.1474 | 0.1244 | 0.1705 |
| budget_reject_rate | overload | 0.09079 | 0.0573 | 0.1243 |
| window_retry_efficiency | retry cost | 0.08126 | 0.06109 | 0.1014 |
| retry_pressure_vs_limit | budget state | 0.0753 | 0.04965 | 0.1009 |
| p95_latency_pressure | overload | 0.06777 | 0.05231 | 0.08324 |
| delta_success_agg | trend | 0.05813 | 0.05259 | 0.06368 |
| server_fail_rate | overload | 0.05031 | 0.04079 | 0.05983 |

**Top controller-memory signals**

| feature | group | combined_rank_score | learned_rank_score | inference_rank_score |
| --- | --- | --- | --- | --- |
| current_min_retry_concurrency_norm | action memory | 0.1736 | 0.1071 | 0.2401 |
| current_percent_norm | action memory | 0.1044 | 0.06521 | 0.1436 |
| previous_percent_norm | action memory | 0.07394 | 0.06982 | 0.07806 |
| previous_min_retry_concurrency_norm | action memory | 0.03543 | 0.0341 | 0.03676 |

## Top Inference-Critical Signals

These features most often changed the selected action, or reduced the probability of the original action, when masked to the training mean.

| feature | group | rank_score | any_action_change_rate | percent_change_rate | min_retry_concurrency_change_rate | mean_joint_prob_drop |
| --- | --- | --- | --- | --- | --- | --- |
| window_load_amplification | retry cost | 0.3821 | 0.5461 | 0.4152 | 0.1669 | 0.4002 |
| current_min_retry_concurrency_norm | action memory | 0.2401 | 0.3311 | 0.227 | 0.255 | 0.1474 |
| delta_window_load_amplification | trend | 0.1705 | 0.235 | 0.1883 | 0.1308 | 0.128 |
| current_percent_norm | action memory | 0.1436 | 0.2163 | 0.1522 | 0.1255 | 0.08051 |
| budget_reject_rate | overload | 0.1243 | 0.1949 | 0.1282 | 0.0761 | 0.09795 |
| window_retry_efficiency | retry cost | 0.1014 | 0.1629 | 0.1121 | 0.06409 | 0.06656 |
| retry_pressure_vs_limit | budget state | 0.1009 | 0.1629 | 0.1215 | 0.05073 | 0.06868 |
| p95_latency_pressure | overload | 0.08324 | 0.1295 | 0.1055 | 0.05474 | 0.04324 |

## Counterfactual Rollout Impact

These rows show what happens when top-ranked signals are masked during closed-loop simulator rollouts. The recovery time is capped for censored/non-recovered runs, so it should be read together with the recovered share.

| mask_feature | recovered_rate | recovery_capped_mean_s |
| --- | --- | --- |
| delta_window_load_amplification | 0.5 | 30.92 |
| window_retry_efficiency | 0.5 | 30.83 |
| retry_pressure_vs_limit | 0.5833 | 29.92 |
| budget_reject_rate | 0.5833 | 29.5 |
| p95_latency_pressure | 0.5833 | 28.83 |
| window_load_amplification | 0.5833 | 28.75 |
| delta_success_agg | 0.75 | 24.92 |

## Metastable vs Generalized Fault Families

The report argues that recovery-time separation is strongest around metastable failures, but the final discussion also asks whether the controller generalizes beyond one sustained fault. The analysis therefore labels scenarios as single-fault, multi-fault, or randomized-metastable and summarizes them separately.

| scenario_scope | scenario_family | runs | recovered_rate | recovery_capped_mean_s | load_amp_mean |
| --- | --- | --- | --- | --- | --- |
| generalized_multi_fault | compound_failure | 5 | 0 | 60 | 4.213 |
| generalized_multi_fault | switchback_adversarial | 5 | 0 | 60 | 5.575 |
| generalized_single_fault | single_load_spike | 5 | 1 | 1.6 | 2.837 |
| generalized_single_fault | single_partial_failure | 5 | 1 | 1 | 1.838 |
| randomized_metastable | randomized_metastable_fairness | 10 | 1 | 4.925 | 1.907 |
| randomized_metastable | randomized_switchback_adversarial | 10 | 0.5 | 38.62 | 3.145 |

## Phase-Specific Telemetry Signals

This table highlights the telemetry signals most correlated with budget actions in each phase and fault scope. It is meant to support discussion figures: what the controller watches during fault, and what changes around recovery.

| scenario_scope | phase | feature | mean_abs_action_corr |
| --- | --- | --- | --- |
| generalized_multi_fault | pre_fault | retry_ratio | 0.9471 |
| generalized_multi_fault | pre_fault | success_rate_agg | 0.9471 |
| generalized_multi_fault | pre_fault | window_load_amplification | 0.9471 |
| generalized_multi_fault | pre_fault | min_client_success | 0.9453 |
| generalized_multi_fault | pre_fault | server_fail_rate | 0.9424 |
| generalized_single_fault | fault | min_client_success | 0.8807 |
| generalized_single_fault | fault | success_rate_agg | 0.8799 |
| generalized_single_fault | fault | window_retry_efficiency | 0.8799 |
| generalized_single_fault | fault | window_load_amplification | 0.8799 |
| generalized_single_fault | fault | retry_ratio | 0.8799 |
| generalized_single_fault | recovery | delta_success_agg | 0.8607 |
| generalized_single_fault | recovery | retry_fairness_gap | 0.856 |
| generalized_single_fault | recovery | delta_window_load_amplification | 0.8445 |
| randomized_metastable | recovery | min_client_success | 0.787 |
| randomized_metastable | recovery | success_rate_agg | 0.7833 |
| randomized_metastable | recovery | window_load_amplification | 0.7794 |

## How To Read The Plots

- `phase_feature_distributions.png`: shows what each observable signal looks like before the fault, during the fault, and during recovery.
- `learned_importance_bar.png`: model-internal ranking from network weights and gradients.
- `inference_occlusion_importance.png`: action sensitivity when each signal is removed at inference.
- `phase_signal_action_correlation_heatmap.png`: phase-specific relationship between signals and chosen budget settings.
- `phase_signal_outcome_correlation_heatmap.png`: phase-specific relationship between signals and recovery/metastability indicators.
- `counterfactual_recovery_impact.png`: closed-loop recovery impact when top signals are masked.
- `fault_family_recovery_summary.png`: recovery behavior split into single-fault, multi-fault, and randomized-metastable groups.
- `telemetry_vs_action_memory_importance.png`: separates true observable system signals from controller memory.
- `top_telemetry_signals_by_phase_scope.png`: compact, report-friendly view of important signals per phase and fault scope.
- `recovery_vs_key_signals.png`: links key signals directly to recovery difficulty.
- `action_distribution_by_phase_family.png`: shows when the model tightens or opens the budget across fault types.
- `timeline_*.png`: representative scenario walkthroughs showing signal movement, budget actions, and recovery.

## Caveats

Correlation plots are explanatory, not causal. The masked-feature counterfactuals are closer to causal evidence, but they still test the trained simulator environment rather than the live cluster. All model-facing analysis uses `VecNormalize`; skipping it would analyze a different input scale than the policy actually sees.
