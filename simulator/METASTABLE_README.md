# Metastable Failure Simulation (Scenario 3)

## Overview

This scenario reproduces **metastable failures** as described in the [OSDI'22 paper](https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf) and [Murat Buffalo's blog post](https://muratbuffalo.blogspot.com/2023/09/metastable-failures-in-wild.html).

A metastable failure occurs when a system enters a degraded state due to a temporary trigger (like a load spike) and **fails to recover** even after the trigger is removed, due to positive feedback loops.

## Key Concepts

### Metastability Triangle
1. **Trigger**: Initial event that pushes system into vulnerable state (e.g., load spike, capacity drop)
2. **Sustaining Effect**: Feedback mechanism that prevents recovery (e.g., retries, queue buildup)
3. **Stable State** → **Vulnerable State** → **Metastable State** (no recovery)

### Positive Feedback Loops Modeled

1. **Queue → Latency**: As queue grows, latency increases
2. **Queue → Capacity Degradation**: Long queues cause resource contention (GC, memory pressure, etc.)
3. **Retries**: Failed/timeout requests trigger retries, amplifying load
4. **Vicious Cycle**: Degraded capacity → more queuing → higher latency → timeouts → retries → even more load

## Simulation Model

### StatefulService
- **Queue**: Requests accumulate when arrival rate exceeds processing capacity
- **Latency**: `latency = base_latency + queue_length * queue_latency_factor`
- **Capacity Degradation**: `capacity = base_capacity * (1 - degradation_factor * queue_length / base_capacity)`
- **Timeouts**: Requests timeout when latency exceeds threshold

### Two Configurations (Same System, Different Retry Policies)

**Key Insight**: Both configurations use the SAME system parameters. The difference in behavior comes entirely from the retry policy, demonstrating how retry amplification can push an otherwise healthy system into metastability.

**System Parameters (identical for both)**:
- Base capacity: Exactly at spike load (no headroom)
- `queue_latency_factor = 0.12` (moderate)
- `capacity_degradation_factor = 0.4` (moderate-high)

#### Recoverable Configuration (No Retries)
- **Retry strategy**: `NoRetries()` - no retry attempts
- **Load amplification**: None (1x)
- **Behavior**: During spike, queue builds up but system maintains equilibrium. After spike ends, queue drains naturally and system returns to normal state.

#### Metastable Configuration (Multiple Retries)
- **Retry strategy**: `NRetries(5)` - 5 retry attempts
- **Load amplification**: Up to 6x (original + 5 retries)
- **Behavior**: During spike, failures trigger retries creating load amplification. The amplified load exceeds capacity, causing queue growth. Queue growth increases latency and degrades capacity (positive feedback loop). Even after spike ends, accumulated queue and degraded capacity prevent recovery - system stuck in metastable state.

## Running the Simulation

```bash
python3 -m simulator.main -s 3 \
    --num_ticks 500 \
    --requests_per_tick 10 \
    --spike_start 100 \
    --spike_end 200 \
    --num_clients 10 \
    --num_retries 2
```

### Parameters
- `--num_ticks`: Simulation duration
- `--requests_per_tick`: Normal load per time tick
- `--spike_start`: When load spike begins
- `--spike_end`: When load spike ends
- `--spike_failure`: Multiplier for load during spike (default: 0.5, used as multiplier)
- `--baseline_failure`: Normal failure rate (default: 0.05)
- `--num_retries`: Number of retry attempts

## Output Plots

### Metastable Comparison
Shows side-by-side comparison:
- **Top**: Latency over time (log scale)
- **Bottom**: Success/fail requests per tick (stacked bar, log scale)
- **Gray region**: Trigger period

### Queue & Capacity Metrics
Additional plots showing:
- Queue length evolution
- Capacity degradation over time

## Key Insights

1. **Retry amplification is the culprit**: With identical system characteristics, different retry policies lead to radically different outcomes
2. **Vulnerability is a spectrum**: Operating at high utilization increases metastability risk
3. **Feedback loops compound**: Queue growth → latency increase → timeouts → retries → more queue growth
4. **Capacity headroom matters**: Even 10% headroom isn't enough when retry amplification is high
5. **Manual intervention needed**: Metastable systems often require load shedding, circuit breakers, or restarts

## Mitigation Strategies

- **Limit retry attempts**: Fewer retries = less load amplification (as demonstrated in recoverable case)
- **Adaptive load shedding**: Drop requests before queues grow too large
- **Circuit breakers**: Stop retries when failure rate exceeds threshold
- **Retry budgets**: Limit total retry amplification across all clients
- **Queue length monitoring**: Watch rate of queue growth, not just absolute size
- **Capacity provisioning**: Maintain headroom that accounts for retry amplification
- **Graceful degradation**: Design systems to maintain some goodput under overload
- **Exponential backoff**: Space out retries to avoid synchronized retry storms

## References

- [Metastable Failures in the Wild (OSDI'22)](https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf)
- [Murat Buffalo's Blog Post](https://muratbuffalo.blogspot.com/2023/09/metastable-failures-in-wild.html)
- [Marc Brooker on Metastability](https://brooker.co.za/blog/2021/05/24/metastable.html)
- [Original GitHub Implementation](https://github.com/lexiangh/Metastability)
