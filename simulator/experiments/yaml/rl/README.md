# RL scenario and benchmark configs

YAML scenarios used by the RL extension. For the full guide see
[`../../../docs/rl/README.md`](../../../docs/rl/README.md). Each scenario declares a
server-side retry budget (`istio_retry_budget` or `global_retry_budget`) plus the
faults / load spikes that create metastable overload. Running a YAML with
`bin/workflow.py` holds the budget static; running it through `bin/rl/...` lets the
agent tune it.

## How this directory is organized

```text
experiments/yaml/rl/
  istio_retry_budget_metastable.yaml      # DIRB final/report Istio training template
  token_bucket.yaml                       # early single-client token-bucket template
  metastable_token_bucket_fairness.yaml   # multi-client token-bucket training template
  istio_retry_budget_benchmarks/          # DIRB / RB-RL.v5 benchmark suite
  metastable_benchmarks/                  # legacy multi-client token-bucket/hybrid suite
  benchmarks/                             # legacy single-client token-bucket suite
```

Use the top-level YAMLs for **training templates**. Use the nested folders for
**benchmark suites**. The same YAML can also be run statically with
`bin/workflow.py` when you want to inspect the simulator without RL.

## Training templates

| File | Used by | Notes |
|------|---------|-------|
| `istio_retry_budget_metastable.yaml` | `bin/rl/train/train_istio_retry_budget_metastable.py` | **Current** Istio training template (also the static-budget benchmark scenario) |
| `token_bucket.yaml` | `bin/rl/train/train_random.py`, `train_relative.py` | Token-bucket training template (earlier work) |
| `metastable_token_bucket_fairness.yaml` | `bin/rl/train/train_metastable_absolute.py`, `train_metastable_relative.py` | Multi-client token-bucket metastable training |

## Benchmark suites

| Folder | Consumed by | Scenarios |
|--------|-------------|-----------|
| `istio_retry_budget_benchmarks/` | `bin/rl/benchmark/run_istio_retry_budget_benchmarks.py` | `istio_partial_failure`, `istio_load_spike`, `istio_compound_failure`, `istio_switchback_adversarial` (default set), plus `istio_adaptive_recovery_diagnostic` |
| `metastable_benchmarks/` | `bin/rl/benchmark/metastable_benchmark_suite.py` (+ `run_metastable_benchmarks_*`) | metastable load-spike, partial-failure, recovery-with-spike, switchback-adversarial, fairness, and static-struggle scenarios |
| `benchmarks/` | `bin/rl/benchmark/run_benchmarks.py`, `run_benchmarks_relative.py` | token-bucket fairness scenarios (`load_spike_fairness`, `partial_failure_fairness`, `recovery_with_spike_fairness`) |

## Current Istio benchmark scenarios

These are the default scenarios used by
`bin/rl/benchmark/run_istio_retry_budget_benchmarks.py`.

| YAML | Fault/load pattern | What it should reveal |
|------|--------------------|-----------------------|
| `istio_partial_failure.yaml` | `svc-A` has a partial failure from 22s to 48s with `p_fail: 0.65`; clients retry up to 4 attempts. | Does the budget prevent retries from turning a transient server fault into queue collapse? |
| `istio_load_spike.yaml` | No server fault; all clients spike from 24s to 54s (`gold x3.4`, `silver x2.9`, `bronze x2.4`). | Does the controller protect the service during pure demand overload and reopen after the spike? |
| `istio_compound_failure.yaml` | Partial failure from 18s to 42s (`p_fail: 0.75`) overlaps with load spikes from 40s to 70s. | Can the policy handle a fault and a traffic surge at the same time? |
| `istio_switchback_adversarial.yaml` | Severe partial failure from 16s to 44s (`p_fail: 0.82`), then later load spikes from 58s to 88s, tighter timeouts, smaller queue. | Can adaptive control beat one static budget when the "right" budget changes between phases? |
| `istio_adaptive_recovery_diagnostic.yaml` | Diagnostic scenario for studying recovery/adaptation behavior. | Useful when debugging why an agent reopens too early/late; not part of the default suite unless selected explicitly. |

The static Istio baseline in these YAMLs is always:

```yaml
istio_retry_budget:
  percent: 20.0
  min_retry_concurrency: 3
```

The RL agent changes only those two server-side fields. Client retry behavior
(`max_attempts`, delay, timeouts, base load) stays fixed by the YAML.

## Scenario families (what each stresses)

- **partial_failure** — the service randomly fails some attempts for a window;
  tests whether the budget stops retries from amplifying a transient failure
  into collapse.
- **load_spike** — traffic multiplies for a window; tests overload protection and
  recovery once the spike ends.
- **compound_failure** — overlapping partial failure and load spike; tests whether
  the model can handle interacting stressors.
- **switchback_adversarial** — a deliberately non-stationary scenario: the early
  phase rewards tightening retries, while the later phase may need different
  admission to preserve useful retries. It is adversarial because any one fixed
  budget is a compromise.
- **fairness / metastable_fairness** — multiple client tiers (gold/silver/bronze)
  share one service; metrics include per-client success and a fairness gap.
- **recovery_with_spike** — recovery after an initial incident is followed by
  another demand spike; tests whether the agent can reopen without immediately
  triggering another retry storm.
- **static_struggle** — a scenario designed to expose the weakness of a single
  static budget.

## What the common YAML fields mean

| Field | Meaning |
|-------|---------|
| `partial_failures` | Service-side fault windows. `p_fail: 0.65` means 65% of attempts fail during that window before retry policy effects. |
| `load_spikes` | Client workload multipliers. `rps_multiplier: 3.0` means the client's arrival rate triples during that window. |
| `retry.max_attempts` | Client-side retry policy. The RL agent does not change this; it controls only the server-side budget. |
| `timeout.attempt_ms` | Per-attempt deadline. Tight deadlines make queueing and tail latency more likely to turn into failed attempts. |
| `queue_capacity` / `workers` | Service capacity model. Smaller queues/tighter workers make overload more metastable. |
| `output_csv` / `fault_events_json` | Static simulator outputs. RL wrappers usually redirect generated benchmark/eval artifacts using `bin/rl/rl_paths.py`. |

## Conventions

- The Istio static baseline is `percent: 20.0`, `min_retry_concurrency: 3`
  (action index `[3, 3]`), matching the benchmark "Static Budget" column.
- Client retry policy (max attempts, backoff) is fixed in the scenario; the agent
  only tunes the **server-side** budget.
