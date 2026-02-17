# How the Service Queue Works

## Simple Explanation

Think of a service like a coffee shop:
- **Customers arrive** (requests)
- **Baristas serve them** (workers)
- **People wait in line** (queue)
- **Shop has limited space** (capacity)

The simulator models this as an **M/G/c/K queue** - a standard model from queueing theory.

## The Four Parts: M/G/c/K

### M - How Customers Arrive (Markovian)

Customers arrive randomly following a **Poisson process**. This means:
- Arrivals are independent
- Average rate is constant (e.g., 100 customers/second)
- Time between arrivals is random (exponential distribution)

**In the simulator:**
```yaml
workload:
  base_rps: 300  # 300 requests per second
```

### G - How Long Service Takes (General)

Service time can follow **any distribution**. The simulator uses **lognormal** because:
- Service times are always positive
- Most requests are fast, but some are slow (long tail)
- Realistic for real services

**In the simulator:**
```yaml
latency:
  median_ms: 20        # Typical request takes 20ms
  lognorm_sigma: 0.5   # Some variation (P99 might be 50ms)
```

**Why not exponential?** Real services don't have exponential latencies - they have long tails!

### c - Number of Workers (Servers)

How many requests can be processed **at the same time**.

**In the simulator:**
```yaml
workers: 16  # Can handle 16 requests simultaneously
```

**Coffee shop analogy:** 16 baristas working in parallel.

### K - Total Capacity (System Size)

Maximum number of requests in the system = **workers + queue**.

**In the simulator:**
```yaml
workers: 16           # 16 being served
queue_capacity: 20    # 20 waiting in line
# Total K = 36
```

**What happens when full?** New requests are **rejected** (load shedding).

---

## How It Works: Step by Step

### 1. Request Arrives
```
Request → Check if worker available?
```

### 2. Worker Available?
```
YES → Start processing immediately
NO  → Check if queue has space
```

### 3. Queue Has Space?
```
YES → Wait in queue (FIFO order)
NO  → REJECT (drop with QUEUE_FULL reason)
```

### 4. Worker Becomes Free
```
Finish current request → Check queue
Queue not empty? → Start next request from queue
```

### 5. Processing
```
Sample service time from lognormal distribution
Schedule completion event
```

### 6. Completion
```
Free the worker
Start next queued request (if any)
Return result to client
```

---

## Visual Example

```
Time: 0ms
Arrivals: [R1, R2, R3, R4, R5, R6, ...]
Workers: [  ] [  ] [  ]  (3 workers, all free)
Queue: []

Time: 10ms
Workers: [R1] [R2] [R3]  (all busy)
Queue: [R4, R5, R6]      (waiting)

Time: 25ms (R1 completes)
Workers: [R4] [R2] [R3]  (R4 moved from queue)
Queue: [R5, R6]

Time: 30ms (R2 completes)
Workers: [R4] [R5] [R3]  (R5 moved from queue)
Queue: [R6]
```

---

## Key Metrics

### Utilization
```
ρ = arrival_rate / (workers × service_rate)
```
- ρ < 1: System stable (can keep up)
- ρ > 1: System overloaded (queue grows)

**Example:**
- Arrival rate: 300 req/s
- Workers: 16
- Service time: 20ms (50 req/s per worker)
- ρ = 300 / (16 × 50) = 0.375 (37.5% utilized)

### Queue Length
Average number of requests waiting.

### Response Time
Total time = **queue wait** + **service time**

### Drop Rate
Percentage of requests rejected due to full queue.

---

## Configuration Examples

### Example 1: High Capacity (Rarely Drops)
```yaml
workers: 20
queue_capacity: 100    # Large queue
latency:
  median_ms: 10
```
- Can handle bursts
- Low drop rate
- Higher memory usage

### Example 2: Low Latency (Small Queue)
```yaml
workers: 50
queue_capacity: 10     # Small queue
latency:
  median_ms: 5
```
- Fast response times
- May drop requests under load
- Lower memory usage

### Example 3: No Queue (Loss System)
```yaml
workers: 10
queue_capacity: 0      # No queue!
latency:
  median_ms: 20
```
- Requests served immediately or rejected
- Predictable latency (no queueing delay)
- Higher drop rate

---

## Common Patterns

### Pattern 1: Overload
```
Arrival rate > Service capacity
→ Queue fills up
→ Requests start dropping
→ Drop rate increases
```

**Solution:** Add more workers or reduce arrival rate

### Pattern 2: Bursty Traffic
```
Normal: 100 req/s → Spike: 500 req/s
→ Queue absorbs burst
→ Drains when traffic returns to normal
```

**Solution:** Size queue for expected burst duration

### Pattern 3: Slow Service
```
Service time increases (e.g., database slow)
→ Workers take longer
→ Queue builds up
→ Response time increases
```

**Solution:** Add workers or fix slow service

---

## Advanced Features

### Deadlines
Requests can timeout while waiting:
```yaml
timeout:
  attempt_ms: 100  # Max 100ms per attempt
```

If a request waits in queue for 100ms, it's dropped with `DEADLINE` reason.

### Fault Injection
Simulate failures:
```yaml
partial_failures:
  - start_s: 30
    end_s: 60
    p_fail: 0.1  # 10% of requests fail
```

### Latency Injection
Simulate slowdowns:
```yaml
latency_injections:
  - start_s: 30
    end_s: 60
    multiplier: 2  # 2x slower
```

---

## Comparison to Real Systems

| Simulator | Real System |
|-----------|-------------|
| `workers` | Thread pool size, CPU cores |
| `queue_capacity` | Request buffer, connection pool |
| `latency` | Database query time, API call |
| `base_rps` | User traffic, API calls/sec |
| `QUEUE_FULL` | 503 Service Unavailable |
| `DEADLINE` | Request timeout |

---

## Tips for Configuration

1. **Start with utilization < 70%**
   - Leaves headroom for bursts
   - Better latency percentiles

2. **Queue size ≈ 2-5× workers**
   - Absorbs small bursts
   - Not too much memory

3. **Monitor drop rate**
   - < 0.1%: Good
   - 0.1-1%: Acceptable
   - > 1%: Need more capacity

4. **Use realistic latencies**
   - Measure your actual service
   - Include P99, not just median

---

## Further Reading

- **Technical details:** See `docs/policy_analysis.md` for policy comparison
- **Queueing theory:** Look up "M/G/c/K queue" or "Kendall's notation"
- **Code:** `src/simulator/runtime/service.py`
