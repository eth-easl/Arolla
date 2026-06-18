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
│   ├── compare_runs.py           # Compare multiple experiments
│   └── rl/                       # RL extension scripts (train/eval/benchmark/analyze)
│
├── src/simulator/                # Core library
│   ├── core/                     # Engine, models, types
│   ├── runtime/                  # Service, client, workload
│   ├── policies/                 # Retry, load limiters, budgets (incl. istio_retry_budget)
│   ├── middleware/               # Composable policy chain
│   ├── faults/                   # Fault injection
│   ├── config/                   # YAML schema & loader
│   ├── metrics/                  # Results collection
│   ├── rl/                       # RL Gymnasium environments
│   └── utils/                    # Time conversion helpers
│
├── experiments/yaml/             # Experiment configurations
│   ├── default.yaml
│   ├── load_spike_metastable_failure/
│   ├── partial_failure_metastable_failure/
│   ├── diversity/
│   └── rl/                       # RL scenario & benchmark configs
│
├── models/RB-RL.v5/              # Committed trained RL model (inference/benchmarks)
├── plotting/                     # Visualization scripts
├── tests/                        # Unit & integration tests
└── docs/                         # Detailed documentation (incl. docs/rl/)
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

## Capacity Planning

Use Little's Law to calculate service headroom:

```
RPS_max = Workers / Mean_Latency
```

For lognormal latency: `Mean = median * e^(sigma^2 / 2)`

**Example:** 16 workers, 20ms median, sigma=0.5:
- Mean = 20ms x 1.133 = 22.66ms
- RPS_max = 16 / 0.02266 = 706 RPS

## Reinforcement-learning extension (optional)

This branch adds **DIRB** (**Dynamic Istio Retry Budget**), the final/report RL
controller that tunes a server-side Istio/Envoy retry budget online to recover
faster from metastable retry amplification. The committed DIRB checkpoint is
`models/RB-RL.v5/`. **RL is off by default**: every command above runs the
simulator statically. RL is only active when you run the scripts under `bin/rl/`.

The RL dependencies (`stable-baselines3`, `gymnasium`, `torch`, `tensorboard`)
are an optional extra, so the core install above stays lightweight:

```bash
pip install -e ".[rl]"    # adds the RL stack on top of the core simulator
```

```bash
# RL off: ordinary static run (retry budget held fixed by the YAML)
python bin/workflow.py experiments/yaml/rl/istio_retry_budget_metastable.yaml

# RL on: evaluate DIRB (the committed RB-RL.v5 model) on the same scenario
python bin/rl/eval/eval_istio_retry_budget_metastable.py \
  --model models/RB-RL.v5/model \
  --yaml experiments/yaml/rl/istio_retry_budget_metastable.yaml \
  --decision-interval-s 5 --observation-window-s 10 --delta-window-s 5
```

Full guide (install, training, inference, benchmarking, model catalog):
[docs/rl/README.md](docs/rl/README.md).

## Testing

```bash
cd simulator
python -m pytest tests/ -v
```

## Documentation

- [Reinforcement-learning extension](docs/rl/README.md) - RL retry-budget controller (training, inference, benchmarking)
