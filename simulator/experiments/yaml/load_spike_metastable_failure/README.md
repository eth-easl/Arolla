# Load Spike Metastability Experiments

We have successfully demonstrated how **Load Spikes** can trigger **Metastable Failures** in systems with aggressive retries, and how **Global Retry Budgets** prevent this.

## Scenarios Overview

| ID | Configuration | Trigger | Outcome |
|----|--------------|---------|---------|
| `ep1` | Safe (No Retries) | Load Spike (3x) | **Recovered** (Load Shedding) |
| `ep2` | Unsafe (Aggressive) | Load Spike (3x) | **FAILED** (Metastable) |
| `ep3` | Static Budget | Load Spike (3x) | **Recovered** (Throttled) |
| `ep4` | AIMD Budget | Load Spike (3x) | **Recovered** (Throttled) |

## Parameters
- **Base Load**: 188 RPS (Stable, ~70% Util)
- **Latenc Median**: 60 ms
- **Timeout**: 192 ms (P99)
- **Queue**: 10000 (Large Buffer)
- **Spike**: 3x Load (564 RPS) for 10s

## Detailed Results

### 1. The Safe Baseline (`ep1`)
**Config**: `max_attempts: 1` (No retries).
**Observation**: The system sheds load effectively during the spike. Since there are no retries, the queue drains immediately after the spike ends.

### 2. Metastable Failure (`ep2`)
**Config**: `max_attempts: 4` (Aggressive retries), `timeout: 192ms`

The trigger: At T=20s, traffic jumped to 564 RPS.
- Immediate Impact: The server couldn't keep up. The unexpected requests poured into the queue.
- Latency Spike: Because the queue is deep, requests didn't get rejected (which would be fast); they sat in line. Wait times skyrocketed past the 192ms timeout.

The Sustaining Loop: Even when the spike ended at T=30s, the system was already trapped.
-  Every new valid request (188 RPS) enters the queue and waits 192ms. By the time it reaches a worker, the client has already timed out. The worker processes it anyway (wasted work), finds it's expired, and drops it.



### 3. Static Global Budget (`ep3`)
**Config**: `ep2` + `global_retry_budget: { target_rps: 30 }`.
**Observation**: The budget prevents the retry storm. During the spike, only a small fraction of retries are allowed. The system survives the spike and recovers.

### 4. Dynamic (AIMD) Global Budget (`ep4`)
**Config**: `ep2` + `aimd_global_retry_budget`.
**Observation**: The AIMD budget detects the high failure rate and throttles retries, preserving system stability.

Here we have a large queue.
In the simulator, the "Queue" is an abstraction. In reality, that 10,000 backlog can exist in several places:
- Async/Non-Blocking Servers (Realistic): Modern frameworks like Go (Goroutines), Node.js, or Java (Netty/WebFlux) do not limit concurrency by thread count. They accept connections as fast as the OS allows and put requests into an internal heap structure. For these systems, having 10,000+ pending requests in memory is trivial and common during spikes. This is the exact scenario determining the metastability.
- Load Balancers (Realistic): If you are behind an AWS ALB (Application Load Balancer) or Nginx, they often buffer requests if the backend is slow, effectively acting as a massive queue.
- Thread-Based Servers (Less Realistic): For older sync architectures (e.g., Rails, Django with Gunicorn, standard Tomcat), the backlog is often limited by the OS socket buffer (e.g., 128 - 2048 connections). In these cases, the system would reject requests (Connection Refused) causing "Fast Failure" rather than "Slow Timeout".