# Global Retry Budget Simulator

A discrete-event simulator for evaluating retry policies and global retry budgets in microservice architectures. Models request processing as an M/G/c/K queue with configurable fault injection, retry strategies, and adaptive budget mechanisms.

## Quick Start

```bash
# Install dependencies
pip install -e .

# Run a single experiment (simulation + plots)
python bin/workflow.py experiments/yaml/default.yaml

# Run with verbose output
python bin/workflow.py experiments/yaml/default.yaml --verbose

# Run a batch of experiments
python bin/workflow.py experiments/yaml/load_spike_metastable_failure/
```

Output is organized automatically:
```
results/default_20251209_084304/
├── default.yaml        # Archived config
├── output.csv          # Time-series metrics
├── fault_events.json   # Fault injection timeline
└── plots/
    ├── latency.png
    ├── qps.png
    ├── queue.png
    ├── failures.png
    └── success_rate.png
```

## Architecture

```
Client(s)                 Service(s)                Policies
┌──────────┐  requests   ┌──────────────┐         ┌─────────────┐
│ Workload ├────────────►│ Workers (c)  │◄────────┤ RetryPolicy │
│ (Poisson)│             │ Queue   (K-c)│         │ LoadLimiter │
│          │◄────────────┤ Faults       │         │ Timeout     │
│          │  responses  │ Middleware   ─┤────────►│ AIMD Budget │
└──────────┘             └──────────────┘         └─────────────┘
                               │
                    ┌──────────┴──────────┐
                    │  Dependency Chain   │
                    │  (optional)         │
                    └─────────────────────┘
```

**Core components:**

| Component | File | Role |
|-----------|------|------|
| Simulator engine | `src/simulator/core/engine.py` | Discrete-event loop with priority queue |
| Service runtime | `src/simulator/runtime/service.py` | M/G/c/K queue with workers, faults, middleware |
| Client runtime | `src/simulator/runtime/client.py` | Request generation, client-side retries |
| Workload driver | `src/simulator/runtime/workload.py` | Poisson arrivals with load spikes |
| Middleware chain | `src/simulator/middleware/` | Composable retry/limiter policy chain |
| Metrics collector | `src/simulator/metrics/collector.py` | Latency percentiles, success rates, CSV export |
| Config loader | `src/simulator/config/loader.py` | YAML-to-simulation builder |

## Project Structure

```
simulator/
├── bin/                          # Entry points
│   ├── workflow.py               # Main: experiment + plots
│   ├── run_experiment.py         # Single experiment runner
│   ├── run_sweep.py              # Parameter sweep runner
│   ├── run_batch.py              # Batch execution
│   └── compare_runs.py           # Compare multiple experiments
│
├── src/simulator/                # Core library
│   ├── core/                     # Engine, models, types
│   ├── runtime/                  # Service, client, workload
│   ├── policies/                 # Retry, load limiters, budgets
│   ├── middleware/               # Composable policy chain
│   ├── faults/                   # Fault injection
│   ├── config/                   # YAML schema & loader
│   ├── metrics/                  # Results collection
│   └── utils/                    # Time conversion helpers
│
├── experiments/yaml/             # Experiment configurations
│   ├── default.yaml
│   ├── load_spike_metastable_failure/
│   ├── partial_failure_metastable_failure/
│   └── diversity/
│
├── plotting/                     # Visualization scripts
├── tests/                        # Unit & integration tests
└── docs/                         # Detailed documentation
```

## Configuration

Experiments are defined declaratively in YAML. Minimal example:

```yaml
name: basic_experiment
seed: 42

services:
  - name: api
    latency:
      median_ms: 20
      lognorm_sigma: 0.5
    workers: 16
    queue_capacity: 20

workload:
  base_rps: 300
  duration_s: 60
```

### Adding Retry Policies

Retries can be configured on the service (server-side) or client (client-side):

```yaml
services:
  - name: api
    # ... service config ...
    retry:
      type: exponential          # none | fixed | exponential | jittered
      max_attempts: 3
      initial_delay_ms: 10
      max_delay_ms: 1000
```

### Adding Global Retry Budget

Server-side token bucket that limits retries globally:

```yaml
services:
  - name: api
    # ... service config ...
    global_retry_budget:
      target_rps: 40             # Allowed retries per second
      max_burst: 10              # Burst capacity
```

### AIMD Adaptive Budget

Self-tuning budget that adjusts based on failure rate feedback:

```yaml
services:
  - name: api
    # ... service config ...
    aimd_global_retry_budget:
      min_rps: 5
      max_rps: 100
      initial_rps: 10
      max_burst: 15
      additive_step: 5           # +5 RPS per healthy window
      decrease_factor: 0.5       # x0.5 on congestion
      window_ms: 1000
      failure_threshold: 0.1     # 10% failure rate triggers backoff
```

### Fault Injection

```yaml
services:
  - name: api
    partial_failures:
      - type: partial_failure
        start_s: 50
        end_s: 70
        p_fail: 0.5             # 50% failure rate

    latency_injections:
      - type: latency_injection
        start_s: 30
        end_s: 60
        multiplier: 3           # 3x latency

workload:
  base_rps: 300
  duration_s: 120
  load_spikes:
    - type: load_spike
      start_s: 20
      end_s: 40
      rps_multiplier: 2.0      # 2x traffic
```

### Service Dependencies

Services can call other services while processing a request. You can configure how these calls are executed, how failures are handled, and when the service performs its own work.

```yaml
services:
  - name: api
    dependencies:
      - service: db
      - service: cache
        optional: true                  # A failure does not fail the request
    dependency_call_pattern: parallel   # sequential (default) | parallel
    dependency_join: fail_fast          # wait_all (default) | fail_fast
    local_work: first                   # last (default) | first
```

- `dependency_call_pattern` controls whether dependencies are called one after another (`sequential`) or at the same time (`parallel`).
- `dependency_join` controls how the service waits for parallel calls. With `wait_all`, it waits for every call to finish. With `fail_fast`, it stops waiting as soon as a required dependency fails. Any other calls already in progress continue running.
- `local_work` controls when the service performs its own work. By default (`last`), it calls its dependencies first. With `first`, it completes its own work before calling them.
- `optional` determines whether a dependency failure causes the request to fail. If set to `true`, a failure of that dependency does not fail the request.

### Exact Arrivals

By default, the workload generates its first request at `t = 0`. Each subsequent request is scheduled using the RPS at the time of the previous request. If the RPS changes before the next request arrives, its scheduled arrival time is not updated. As a result, short load spikes may be missed entirely.

Setting `exact_arrivals: true` changes this behavior:

- The first request arrives after a random delay instead of immediately at `t = 0`.
- Changes in RPS take effect at their configured times, even when they occur between requests.
- Request arrivals remain random, but their timing correctly accounts for load spikes.

```yaml
workload:
  base_rps: 300
  duration_s: 120
  exact_arrivals: true
```

Use `exact_arrivals: true` when you want traffic generation to accurately reflect short or rapidly changing load spikes.

### Multi-Client Mode

```yaml
clients:
  - name: client_a
    target_service: api
    retry:
      type: exponential
      max_attempts: 3
      initial_delay_ms: 10
      max_delay_ms: 100
    workload:
      base_rps: 100
      duration_s: 60

  - name: client_b
    replicas: 3
    target_service: api
    workload:
      base_rps: 50
      duration_s: 60
```

### Deadline Propagation

Deadline propagation controls whether a service and its dependencies stop processing a request when the caller's timeout expires.

By default, when a client times out, it stops waiting for a response, but the service continues processing the request. This can waste resources on requests whose results are no longer needed.

Setting `propagate_deadline: true` on a client changes this behavior. The client passes its deadline to the service, which stops processing the request when that deadline is reached.

```yaml
clients:
  - name: client_a
    target_service: api
    timeout:
      attempt_ms: 100
    propagate_deadline: true
```

In this example, if the request takes longer than 100 ms, the client times out and the service stops processing it.

**Deadline propagation between services**

By default, services also pass their own attempt deadlines to their dependencies. This ensures that dependencies stop working when the calling service's deadline expires.

Setting `propagate_deadline: false` on a service disables this behavior. The service still stops waiting when its own deadline expires, but its dependencies continue processing their calls, even though their results are no longer needed.

### Parameter Sweeps

Use inline lists to sweep over parameter values:

```yaml
services:
  - name: api
    partial_failures:
      - type: partial_failure
        start_s: 50
        end_s: 70
        p_fail: [0.1, 0.3, 0.5, 0.7]  # Sweep over failure rates
```

Run with: `python bin/workflow.py config.yaml` (auto-detects sweeps).

## Running Experiments

| Command | Purpose |
|---------|---------|
| `python bin/workflow.py <yaml>` | Full workflow: simulate + plot |
| `python bin/workflow.py <dir/>` | Batch: run all YAMLs in directory |
| `python bin/run_experiment.py <yaml>` | Simulation only (no plots) |
| `python bin/run_experiment.py <yaml> --validate-only` | Validate config without running |
| `python bin/run_sweep.py <yaml>` | Parameter sweep only |

## Tests

```bash
# Unit tests (from simulator/)
PYTHONPATH=src python -m pytest -q

# Scenario tests (from the repository root)
git worktree add ../arolla-main main
python simulator/bin/check_scenarios.py ../arolla-main .
```

`bin/check_scenarios.py` runs all scenarios in `experiments/yaml` using both versions of the code and
reports which scenarios produce different results. 
GitHub Actions runs both checks on every pull request that modifies `simulator/`.
If a change affects the results of a scenario, the scenario check fails. The pull request must document
which scenarios are affected and why.

## Capacity Planning

Use Little's Law to calculate service headroom:

```
RPS_max = Workers / Mean_Latency
```

For lognormal latency: `Mean = median * e^(sigma^2 / 2)`

**Example:** 16 workers, 20ms median, sigma=0.5:
- Mean = 20ms x 1.133 = 22.66ms
- RPS_max = 16 / 0.02266 = 706 RPS
