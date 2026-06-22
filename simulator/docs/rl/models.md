# RL models: what each one is, its reward, and how to run it

This is the per-model reference for the RL extension. Each "model" here is a
**PPO policy trained against one Gymnasium environment**. The environment defines
three things that fully describe the model:

1. **Control surface** — the action space (what the agent is allowed to change).
2. **Observation** — the features the agent sees each decision interval.
3. **Reward** — the scalar signal it is trained to maximize.

The control surface evolved over the project. The **final model type used in the
report is named DIRB, for Dynamic Istio Retry Budget**. In this repository, the
shipped DIRB checkpoint is `RB-RL.v5`. Its environment is
`IstioRetryBudgetMetastableEnv`, controlling server-side
`retryBudget.percent` and `retryBudget.minRetryConcurrency`. The earlier
token-bucket and hybrid models are legacy research steps; they are kept because
they document how the design got there and are still runnable.

> Notation used below for rewards:
> `success_rate` = fraction of attempts that succeeded in the window,
> `retry_ratio` = retries / attempts, `deadline_rate` / `queue_fail_rate` /
> `server_fail_rate` = fraction of attempts failing that way,
> `latency_pressure` = p95 latency relative to the no-load baseline,
> `Δsuccess` = change in success vs. the previous window,
> `action_distance` = how far this action moved the knobs,
> `counteracting_change` / `action_reversal` = the agent undoing its last move.

| # | Model family | Env class | File | Control surface | Status |
|---|--------------|-----------|------|-----------------|--------|
| 1 | **DIRB: Dynamic Istio Retry Budget** | `IstioRetryBudgetMetastableEnv` | `istio_retry_budget_env.py` | `percent`, `minRetryConcurrency` | **Final report model / shipped as `RB-RL.v5`** |
| 2 | Token bucket (single client) | `RandomScenarioSimEnv` | `random_scenario_env.py` | `refill_rate`, `bucket_capacity` | Legacy |
| 3 | Token bucket (multi-client / fairness) | `MetastableFairnessSimEnv` | `metastable_fairness_env.py` | `refill_rate`, `bucket_capacity` | Legacy |
| 4 | Relative-action variants | `RelativeAction*` | `relative_action_env.py`, `metastable_relative_action_env.py` | +/-1 step on each knob | Legacy |
| 5 | Hybrid (3-knob) | `Hybrid*MetastableFairnessEnv` | `hybrid_metastable_env.py` | + per-success token reward | Legacy |
| 6 | Proof of concept | `RetrySimEnv` | `env.py` | `refill_rate`, `bucket_capacity` | Smoke test only |

All env classes live in `simulator/src/simulator/rl/`. Every env also exposes
`observation_space_description()`, `action_space_description()`, and
`reward_function_description()` so you can print the contract at runtime.

---

## 1. DIRB — Dynamic Istio Retry Budget (final report model)

**What it is.** This is the **final model family used for the report**. The
report calls it **DIRB** (**Dynamic Istio Retry Budget**); this repository ships
the trained checkpoint as [`RB-RL.v5`](../../models/RB-RL.v5/README.md). It is a
server-side controller that tunes an Istio/Envoy-style retry budget online during
a metastable incident.

**Control surface** — `MultiDiscrete([6, 6])`:

- `action[0]` → `retryBudget.percent` from `[0, 5, 10, 20, 35, 50]`
- `action[1]` → `retryBudget.minRetryConcurrency` from `[0, 1, 2, 3, 5, 8]`

**Observation** — 18 caller-side, per-attempt features (fixed order):
`success_rate_agg, min_client_success, retry_ratio, window_load_amplification,
window_retry_efficiency, retry_fairness_gap, p95_latency_pressure,
budget_reject_rate, server_fail_rate, deadline_rate, delta_success_agg,
delta_window_load_amplification, budget_utilization, retry_pressure_vs_limit,
current_percent_norm, current_min_retry_concurrency_norm, previous_percent_norm,
previous_min_retry_concurrency_norm`.

**Reward** (`istio_retry_budget_reward`):

```text
overload        = max(deadline_rate, queue_fail_rate, tail/3, budget_reject)
retry_storm     = retry_ratio * overload
budget_saturation = max(0, budget_utilization - 0.85)
tail            = min(latency_pressure, 3)

reward =  2.2 * success_rate
       +  0.35 * max(Δsuccess, 0)        # reward recovery progress
       -  0.45 * retry_ratio             # discourage retries in general
       -  0.85 * retry_storm             # punish retries *while overloaded*
       -  0.65 * deadline_rate
       -  0.50 * queue_fail_rate
       -  0.20 * server_fail_rate
       -  0.25 * tail
       -  0.25 * budget_reject_rate
       -  0.20 * budget_saturation
       -  0.08 * max(Δretry_pressure, 0)
       -  0.05 * action_distance         # prefer small adjustments
       -  0.04 * action_reversal         # don't oscillate
```

**How it works.** The reward deliberately uses only server-/caller-side signals
(no global benchmark metrics), so the same policy can drive a real in-cluster
controller. It rewards keeping attempts succeeding and recovering quickly, while
heavily penalizing retries *during overload* (the `retry_storm` term), which is
exactly the metastable-amplification behaviour we want to suppress.

**How to run** (from `simulator/`, using the shipped model):

```bash
# Inference / evaluation on the training scenario
python bin/rl/eval/eval_istio_retry_budget_metastable.py \
  --model models/RB-RL.v5/model \
  --yaml experiments/yaml/rl/istio_retry_budget_metastable.yaml \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5

# Benchmark vs no-budget and static-budget baselines
python bin/rl/benchmark/run_istio_retry_budget_benchmarks.py \
  --model models/RB-RL.v5/model \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5

# Train your own
python bin/rl/train/train_istio_retry_budget_metastable.py \
  --timesteps 1000000 --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5
```

---

## 2. Token bucket, single client — `RandomScenarioSimEnv` (legacy)

**What it is.** The first "real" controller: it sizes a global retry-budget
**token bucket** on a single-client service across randomized fault/load
scenarios.

**Control surface** — `MultiDiscrete([5, 5])`:

- `action[0]` → `refill_rate` from `[5, 15, 30, 60, 90]` rps
- `action[1]` → `bucket_capacity` from `[5, 10, 20, 50, 80]` tokens

**Observation** — 15 features: health (`success_rate`, `retry_ratio`,
`latency_pressure`, `queue_utilization`, fail rates, `deadline_rate`), trends
(`delta_*`), and budget state (`bucket_fill_ratio`, pressure-vs-refill, last
action indices).

**Reward** (`simple_reward`):

```text
reward = 1.50 * success_rate
       - 0.20 * retry_ratio
       - 0.65 * deadline_rate
       - 0.25 * queue_fail_rate
       - 0.04 * min(latency_pressure, 3)
       + 0.15 * Δsuccess
       - 0.02 * action_distance
       - 0.04 * counteracting_change
```

**How to run:**

```bash
python bin/rl/train/train_random.py --timesteps 500000
python bin/rl/eval/eval_rl_scenario.py --model trained_models/run_.../model --yaml <scenario.yaml>
python bin/rl/benchmark/run_benchmarks.py --model trained_models/run_.../model
```

---

## 3. Token bucket, multi-client fairness — `MetastableFairnessSimEnv` (legacy)

**What it is.** Same token-bucket knobs as #2, but with **multiple clients
sharing one budget** and explicit fault/recovery windows. It introduces fairness
and recovery-aware reward shaping.

**Control surface** — `MultiDiscrete([5, 5])` (same refill/capacity grids as #2).

**Observation** — 21 features: the single-client signals plus per-window
aggregates (`window_success_agg`, `window_min_client_success`,
`window_load_amplification`, `window_retry_efficiency`, `window_fairness_gap`)
and `fault_active` / `recovery_active` flags.

**Reward** (`metastable_reward`) — a base term plus phase-specific bonuses:

```text
base =  1.35 * agg_success
     +  0.85 * min_client_success      # protect the worst-off client (fairness)
     +  0.40 * retry_efficiency
     -  0.42 * max(load_amplification - 1, 0)
     -  0.30 * fairness_gap
     -  0.30 * deadline_rate
     -  0.12 * queue_fail_rate
     -  0.14 * min(latency_pressure, 3)
     -  0.03 * action_distance
     -  0.04 * counteracting_change

if fault_active:    base += 0.70*agg_success + 0.45*min_client_success
                          + 0.20*retry_efficiency - 0.22*amp_excess - 0.08*tail
if recovery_active: base += 0.75*max(Δsuccess,0) + 0.25*success_rate
                          + 0.30*agg_success - 0.10*amp_excess
else:               base += 0.10*Δsuccess
```

**How to run:**

```bash
python bin/rl/train/train_metastable_absolute.py --timesteps 1000000
python bin/rl/eval/eval_rl_metastable_absolute.py --model trained_models/metastable_absolute_action/run_.../model --yaml <scenario.yaml>
python bin/rl/benchmark/run_metastable_benchmarks_absolute.py --model trained_models/metastable_absolute_action/run_.../model
```

---

## 4. Relative-action variants (legacy)

**What it is.** Same observations and rewards as #2 / #3, but the agent emits
**relative moves** instead of absolute settings — it nudges each knob up/down/keep
by one grid step. This makes control smoother and avoids large jumps.

- `RelativeActionRandomScenarioSimEnv` (single client) → reuses `simple_reward`.
- `RelativeActionMetastableFairnessEnv` (multi-client) → reuses `metastable_reward`.

**Control surface** — `MultiDiscrete([3, 3])`: each entry is `{down, keep, up}`
applied to the current `refill_rate` / `bucket_capacity` index.

**How to run:**

```bash
python bin/rl/train/train_relative.py --timesteps 500000          # single client
python bin/rl/train/train_metastable_relative.py --timesteps 1000000   # multi-client
python bin/rl/benchmark/run_benchmarks_relative.py --model trained_models/relative_action/run_.../model
python bin/rl/benchmark/run_metastable_benchmarks_relative.py --model trained_models/metastable_relative_action/run_.../model
```

---

## 5. Hybrid 3-knob — `Hybrid*MetastableFairnessEnv` (legacy)

**What it is.** Extends the metastable token bucket (#3) with a **third knob: an
event-based token reward** — how many tokens to add to the bucket per successful
attempt. This combines a time-based refill with success-driven refills.

- `HybridMetastableFairnessEnv` — absolute, `MultiDiscrete([5, 5, 5])`:
  `action[2]` → `event_reward` from `[0.0, 0.05, 0.15, 0.30, 0.60]`.
- `HybridRelativeActionMetastableFairnessEnv` — relative, `MultiDiscrete([3, 3, 3])`.

**Observation** — the 21 metastable features plus 1 normalized event-reward index
(22 total).

**Reward** — `metastable_reward` (as in #3) minus a small churn penalty
(`0.005`) whenever the agent changes the event-reward knob, to discourage
needless flapping of the third control.

**How to run:**

```bash
python bin/rl/train/train_metastable_hybrid_absolute.py --timesteps 1000000
python bin/rl/train/train_metastable_hybrid_relative.py --timesteps 1000000
python bin/rl/eval/eval_rl_metastable_hybrid.py --model trained_models/metastable_hybrid_absolute_action/run_.../model --yaml <scenario.yaml>
python bin/rl/benchmark/run_metastable_benchmarks_hybrid.py --model trained_models/metastable_hybrid_absolute_action/run_.../model
```

---

## 6. Proof of concept — `RetrySimEnv` (smoke test only)

**What it is.** The very first experiment: a 13-feature observation, a token
bucket `MultiDiscrete([5, 5])` action, and a minimal reward
`success_rate − 0.3 * retry_ratio`. It exists to validate the Gymnasium wiring,
not to produce a usable controller. The smoke trainers `train_rl_test.py` and
`train_random_test.py` exercise it and write throwaway models to
`trained_models/_smoke/` (git-ignored).

---

## Where to go next

- Exact tensor shapes and ordering: [`action_observation_spaces.md`](action_observation_spaces.md)
- The shipped model's full card: [`../../models/RB-RL.v5/README.md`](../../models/RB-RL.v5/README.md)
- Output locations and run conventions: [`../../bin/rl/README.md`](../../bin/rl/README.md)
