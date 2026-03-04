# Global Retry Budget Simulator

First two scenarios reproduce the results in the [Marc Brooker's blog](https://marcbrooker.com/2025/09/11/retry-budgets.html). 

Scenario 3 reproduces metastable failures as described in the [OSDI'22 paper "Metastable Failures in the Wild"](https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf) and [Murat Buffalo's blog post](https://muratbuffalo.blogspot.com/2023/09/metastable-failures-in-wild.html).

## Scenarios

### Scenario 1: Basic Retry Strategy Comparison
Compare different retry strategies under varying server failure rates (0-10%):
- No retries
- Fixed retries
- Circuit breaker
- Token bucket (adaptive retries)

```bash
python3 -m simulator.main -s 1 --num_clients 100 --requests_per_client 500
```

### Scenario 2: Client Count Effect
Examine how the number of clients affects token bucket and circuit breaker strategies.

```bash
python3 -m simulator.main -s 2 --client_counts "10,100,1000" --total_requests 50000
```

### Scenario 3: Metastable Failure
Demonstrate systems that recover vs. get stuck in metastable state after a load spike.

```bash
python3 -m simulator.main -s 3 --num_ticks 500 --requests_per_tick 10 \
    --spike_start 100 --spike_end 200 --num_clients 10 --num_retries 2
```

See [METASTABLE_README.md](../METASTABLE_README.md) for detailed documentation on metastable failures.

## Requirements

```bash
pip install matplotlib numpy
```
