# Istio retry-budget RL: action and observation spaces

This document describes the current **action space** and **observation space** for the
`IstioRetryBudgetMetastableEnv` PPO model (`simulator/src/simulator/rl/istio_retry_budget_env.py`).

The agent tunes two Istio/Envoy retry-budget fields on the **server** at runtime:

- `retryBudget.percent`
- `retryBudget.minRetryConcurrency`

Client retry policy (max attempts, backoff, etc.) stays fixed in the scenario YAML.

---

## Environment summary

| Property | Value |
|----------|-------|
| Env class | `IstioRetryBudgetMetastableEnv` |
| Policy | PPO (`MlpPolicy`) |
| Observation | `Box(18,)`, `float32` |
| Action | `MultiDiscrete([6, 6])` → **36** joint settings |
| Action type | **Absolute** selection (not relative ±1) |
| Feature source | **Caller-side**, per-**attempt** (`client.roots[*].attempts[*]`) |
| Training script | `simulator/bin/train_istio_retry_budget_metastable.py` |
| Eval / benchmarks | `simulator/bin/eval_istio_retry_budget_metastable.py`, `run_istio_retry_budget_benchmarks.py` |

### Timing parameters (must match at train and inference)

These are **constructor arguments**, not part of the vector itself:

| Parameter | CLI flag | Default | Role |
|-----------|----------|---------|------|
| Decision interval | `--decision-interval-s` | `2.0` | Seconds between actions |
| Observation window | `--observation-window-s` | decision interval | Window for features #1–10, #13–14 |
| Delta window | `--delta-window-s` | observation window | Window for delta features #11–12 |

Example (5 s reactiveness, 10 s obs, 5 s delta):

```bash
python bin/train_istio_retry_budget_metastable.py \
  --decision-interval-s 5 \
  --observation-window-s 10 \
  --delta-window-s 5
```

Use the **same three values** when running eval, benchmarks, and the live prototype controller.
Ship `vecnormalize_stats.pkl` from the same training run next to `model.zip`.

---

## Action space

```python
action_space = MultiDiscrete([6, 6])
```

Each step selects one index per dimension:

### Dimension 0 — `retryBudget.percent`

| Index | Percent |
|------:|--------:|
| 0 | 0% |
| 1 | 5% |
| 2 | 10% |
| 3 | **20%** ← static benchmark baseline |
| 4 | 35% |
| 5 | 50% |

### Dimension 1 — `minRetryConcurrency`

| Index | Min concurrency |
|------:|----------------:|
| 0 | 0 |
| 1 | 1 |
| 2 | 2 |
| 3 | **3** ← static benchmark baseline |
| 4 | 5 |
| 5 | 8 |

### Default / reset alignment

Benchmark YAMLs and the static baseline use **`(20%, 3)`** → action indices **`[3, 3]`**.

Training episodes may randomize the initial budget, but benchmarks reset from YAML and sync
action memory on `reset()`.

### Effect on the service

Selected values update `IstioRetryBudget` via `service.update_istio_retry_budget()`.
The server applies Istio-style concurrency semantics:

```text
concurrency_limit = max(min_retry_concurrency, percent / 100 × (active_requests + pending_requests))
```

A retry is admitted when `active_retries < concurrency_limit`.

Because of the `max(...)`, low load is often **floor-dominated** (min matters most) and high
load is **percent-dominated**.

---

## Observation space

```python
observation_space = Box(low=-inf, high=inf, shape=(18,), dtype=float32)
```

The observation is an **18-dimensional float vector** the agent sees at every decision step.
It is designed to mirror what the **live Istio/Envoy prototype controller** can scrape from
**caller-side** stats (outbound cluster metrics on the loader), not what the server knows
internally.

### Design principle: caller-side, per-attempt

| Principle | Meaning |
|-----------|---------|
| **Caller-side** | Features come from `client.roots[*].attempts[*]` (simulated loader view), not from `service.live_buffer` |
| **Per-attempt** | Each HTTP attempt counts; a root request with 4 tries contributes 4 samples, not 1 |
| **Windowed** | Most features aggregate over the last `observation_window_s` seconds |
| **Normalized at inference** | Raw vector → `VecNormalize` → policy; stats must match training |

This alignment lets a model trained in the simulator deploy on the prototype without a
distribution shift from “server oracle” features the mesh cannot expose.

### Feature groups

```text
  Client health (0–1)     Retry cost (2–5)        Overload (6–9)
  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
  │ success agg     │    │ retry_ratio     │    │ p95 pressure    │
  │ min client succ │    │ load_amp        │    │ budget_reject   │
  └─────────────────┘    │ retry_eff       │    │ server_fail     │
                         │ fairness_gap    │    │ deadline        │
                         └─────────────────┘    └─────────────────┘

  Trends (10–11)          Budget state (12–13)    Action memory (14–17)
  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
  │ delta_success   │    │ budget_util     │    │ current % / min │
  │ delta_load_amp  │    │ retry_pressure  │    │ previous % / min│
  └─────────────────┘    └─────────────────┘    └─────────────────┘
```

### Full feature table

All caller-derivable features are computed in `istio_caller_window_metrics()` over completed
attempts in `[now - window, now]`. Budget state (#12–13, #14–15) comes from
`IstioRetryBudget` on the service.

| # | Name | Description | Formula / source |
|---|------|-------------|------------------|
| 0 | `success_rate_agg` | Aggregate attempt success | `successful_attempts / total_attempts` (per-attempt, all clients) |
| 1 | `min_client_success` | Worst client attempt success | `min over clients of (client_successful_attempts / client_attempts)` |
| 2 | `retry_ratio` | Retry share of traffic | `retry_attempts / total_attempts` |
| 3 | `window_load_amplification` | Attempt amplification | `total_attempts / first_attempts` (= attempts / roots in window) |
| 4 | `window_retry_efficiency` | Useful retries | `successful_retries / total_retries` |
| 5 | `retry_fairness_gap` | Retry share imbalance | `0.5 × Σ \|retry_share − first_attempt_share\|` over clients; shares observed in window |
| 6 | `p95_latency_pressure` | Tail latency vs timeout | `p95(attempt_latency_ms) / attempt_timeout_ms` (no clamp) |
| 7 | `budget_reject_rate` | Budget rejections | `rejected_retries / (rejected_retries + admitted_retries)` in window |
| 8 | `server_fail_rate` | Server-side failures | Fraction of attempts with `SERVER_FAILURE` or `QUEUE_FULL` |
| 9 | `deadline_rate` | Timeouts | Fraction with `DEADLINE` or `latency_ms ≥ attempt_timeout_ms` |
| 10 | `delta_success_agg` | Success trend | `success_rate_agg(delta_window) − previous_success_rate_agg` |
| 11 | `delta_window_load_amplification` | Amplification trend | `load_amp(delta_window) − previous_load_amp` |
| 12 | `budget_utilization` | Budget saturation | `active_retries / concurrency_limit` (instantaneous gauge) |
| 13 | `retry_pressure_vs_limit` | Retry rate vs cap | `(retry_attempts_in_window / window_s) / concurrency_limit` |
| 14 | `current_percent_norm` | Current action memory | `percent / 100` |
| 15 | `current_min_retry_concurrency_norm` | Current action memory | `min_retry_concurrency / 10` |
| 16 | `previous_percent_norm` | Previous action memory | `previous_percent / 100` |
| 17 | `previous_min_retry_concurrency_norm` | Previous action memory | `previous_min_retry_concurrency / 10` |

### Notes

- **Per-attempt, not per-root:** features #0–2 and #6–10 aggregate over `client.roots[*].attempts[*]`,
  not over logical root requests. This matches the live Envoy/caller-side controller.
- **`budget_reject_rate` (#7):** replaces the old server `queue_utilization` feature. Rejections are
  recorded on `IstioRetryBudget` when client-managed retries hit the budget gate in
  `service.submit_request()`.
- **Empty windows:** caller metrics fall back to the previous step’s success rate and retry ratio.
- **Inference:** apply `VecNormalize.load(vecnormalize_stats.pkl)` to the raw 18-D vector before
  `model.predict()`. Stats must come from the same training run and window settings as the model.

---

## Observation space: what changed

The vector is still **18 floats in the same slot order**, but several features changed
**data source**, **formula**, or **meaning**. Old checkpoints and `vecnormalize_stats.pkl`
files are **not compatible** with the current env.

### High-level changes

| Aspect | Before | After |
|--------|--------|-------|
| **Shape** | `Box(18,)` | `Box(18,)` (unchanged) |
| **Data path** | Hybrid: client metrics + `service.live_buffer` | **All caller-derivable features from client attempt tree** |
| **Unit of aggregation** | Mixed: per-root for success/latency; server for failures | **Per-attempt everywhere** for #0–2, #6–10 |
| **Feature #7** | `queue_utilization` = `queue_avg / queue_capacity` (server) | **`budget_reject_rate`** = rejected / (rejected + admitted) retries |
| **Fairness (#5)** | vs configured **offered RPS share** | vs observed **first_attempt_share** in window |
| **p95 pressure (#6)** | Per-root span p95, clamped in reward | Per-attempt p95, **no clamp** in observation |
| **Instrumentation** | None for budget rejects | `IstioRetryBudget.record_retry_admission()` |
| **Prototype alignment** | Partial (server features not observable in mesh) | Matched to Envoy caller-side stats |

### Per-feature changelog

| # | Feature | Before | After |
|---|---------|--------|-------|
| 0 | `success_rate_agg` | Per-**root** success over completed roots in window | Per-**attempt** success: `Σ success / Σ attempts` |
| 1 | `min_client_success` | Per-root success, min over clients | Per-**attempt** success, min over clients |
| 2 | `retry_ratio` | `service.live_buffer` retry ratio | Caller: `retry_attempts / total_attempts` |
| 3 | `window_load_amplification` | Client tree (`attempts / roots`) | Same formula; now strictly per-attempt counting |
| 4 | `window_retry_efficiency` | Client tree | Same formula; caller-side |
| 5 | `retry_fairness_gap` | `\|retry_share − offered_RPS_share\|` | `\|retry_share − first_attempt_share\|` (observed in window) |
| 6 | `p95_latency_pressure` | p95 of **root** end-to-end latency / timeout | p95 of **attempt** latencies / timeout; unclamped |
| 7 | **`queue_utilization` → `budget_reject_rate`** | Server queue fill ratio | Fraction of caller-requested retries **rejected by budget** |
| 8 | `server_fail_rate` | Server `live_buffer` failure breakdown | Caller attempts: `SERVER_FAILURE` + `QUEUE_FULL` |
| 9 | `deadline_rate` | Server `live_buffer` deadline count | Caller attempts: `DEADLINE` or `latency ≥ timeout` |
| 10 | `delta_success_agg` | Delta of old #0 | Delta of new per-attempt success |
| 11 | `delta_window_load_amplification` | Unchanged logic | Fed by new caller load_amp |
| 12 | `budget_utilization` | `IstioRetryBudget.utilization` | Unchanged |
| 13 | `retry_pressure_vs_limit` | `retry_rps / concurrency_limit` (server buffer retries) | Same form; retry count from **caller** window |
| 14–17 | Action memory | `percent/100`, `min/10` | Unchanged |

### Why #7 changed (most important semantic shift)

**Old `queue_utilization`** measured how full the **server worker queue** was. That is useful
for server-side overload but **not observable** on the caller’s Envoy outbound cluster in the
live prototype.

**New `budget_reject_rate`** measures what the controller can actually see once
`upstream_rq_retry_overflow` (or equivalent) is wired:

```text
budget_reject_rate = rejected_retries / (rejected_retries + admitted_retries)
```

High values mean the budget is actively blocking retries the client wanted to send — a direct
signal for “tighten or loosen the budget” without peeking at server queue depth.

### Reward coupling (also changed)

The reward overload term now uses **`budget_reject_rate`** instead of queue utilization.
Other reward inputs also pull from caller-side metrics where they previously used
`live_buffer` success/failure rates.

---

## Reward (for context)

The reward uses caller-side metrics where noted and penalizes action churn:

- Rewarded: attempt success, recovery progress
- Penalized: retry ratio, retry storms under overload, deadlines, queue failures, server failures,
  tail pressure, budget rejections, budget saturation, retry-pressure growth, large action jumps,
  action reversals

See `istio_retry_budget_reward()` in `istio_retry_budget_env.py` for coefficients.

---

## Code references

| Item | Location |
|------|----------|
| Action maps | `PERCENT_MAP`, `MIN_RETRY_CONCURRENCY_MAP` in `istio_retry_budget_env.py` |
| Observation builder | `build_istio_metastable_observation_vector()` |
| Caller metrics | `istio_caller_window_metrics()` |
| Budget reject tracking | `IstioRetryBudget.record_retry_admission()` in `policies/istio_retry_budget.py` |
| Training | `simulator/bin/train_istio_retry_budget_metastable.py` |

---

## Compatibility

Models trained before the current spaces are **not compatible**:

- Action space changed from `MultiDiscrete([5, 5])` to `MultiDiscrete([6, 6])`
- Observation **shape** unchanged (`18`) but **semantics** changed for features #0–2, #5–10, #13, and #7 renamed/redefined
- Old `vecnormalize_stats.pkl` encodes the previous distribution — do not reuse

Retrain and ship a new `vecnormalize_stats.pkl` with any new `model.zip`.
