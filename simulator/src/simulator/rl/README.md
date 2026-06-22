# `simulator.rl` — RL environments

Gymnasium environments that wrap the discrete-event simulator so a PPO agent can
tune a server-side retry budget online. This package is the **library**; the
runnable scripts (train / eval / benchmark / analyze) live in
[`../../../bin/rl/`](../../../bin/rl) and the full guide is in
[`../../../docs/rl/README.md`](../../../docs/rl/README.md).

In other words:

- `src/simulator/rl/` answers **"what is the RL environment?"**
- `bin/rl/` answers **"how do I run training/eval/benchmarks?"**
- `experiments/yaml/rl/` answers **"what scenario is the environment running?"**

## Environments

| Module | Control surface | Role |
|--------|-----------------|------|
| `istio_retry_budget_env.py` | Istio `percent`, `minRetryConcurrency` | **DIRB final/report environment**; Dynamic Istio Retry Budget, matching the live Istio prototype and producing `RB-RL.v5`. |
| `metastable_fairness_env.py` | token bucket `refill_rate`, `bucket_capacity` | Multi-client fairness metastable scenarios with gold/silver/bronze clients. |
| `relative_action_env.py` | token bucket relative moves | Earlier single-client variant where actions nudge knobs down/keep/up instead of selecting absolute values. |
| `metastable_relative_action_env.py` | token bucket relative moves | Relative-action version of the multi-client metastable environment. |
| `hybrid_metastable_env.py` | token bucket plus event reward | Three-knob hybrid budget experiments; adds tokens on successful attempts. |
| `hybrid_retry_budget.py` | hybrid budget helper | Policy/runtime helper used by `hybrid_metastable_env.py`. |
| `random_scenario_env.py` | token bucket absolute selection | Earlier randomized-scenario token-bucket training environment. |
| `env.py` | token bucket absolute selection | First proof-of-concept environment; useful mainly for smoke tests. |

All environments expose the standard Gymnasium `reset()` / `step()` API. Each
`step()` advances the simulator by one decision interval, builds the observation
from recent metrics, applies the chosen action to the service's retry budget, and
returns a reward.

## Scenario profiles

Some environments accept `scenario_profile`:

- `metastable_fairness` is the default profile. It randomizes multi-client
  incidents around fairness and recovery: one shared service, several clients,
  and a retry budget that must protect both aggregate success and the worst-off
  client.
- `switchback_adversarial` is a harder non-stationary profile. The run contains
  phases where different budget behavior is preferable, so a fixed static budget
  is forced to compromise. This is useful for testing whether adaptive RL control
  really uses timing and telemetry rather than learning one static setting.

## Where to go next

- Action and observation contract: [`../../../docs/rl/action_observation_spaces.md`](../../../docs/rl/action_observation_spaces.md)
- Trained model + commands: [`../../../models/RB-RL.v5/README.md`](../../../models/RB-RL.v5/README.md)
- The Istio budget limiter the env mutates: `../policies/istio_retry_budget.py`
