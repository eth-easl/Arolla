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
- **Queue**: 10000 (Infinite Buffer)
- **Spike**: 3x Load (564 RPS) for 10s

## Detailed Results

### 1. The Safe Baseline (`ep1`)
**Config**: `max_attempts: 1` (No retries).
**Observation**: The system sheds load effectively during the spike. Since there are no retries, the queue drains immediately after the spike ends.

### 2. Metastable Failure (`ep2`)
**Config**: `max_attempts: 4` (Aggressive retries), `timeout: 192ms`.
**Observation**: The load spike causes an initial wave of timeouts. The aggressive retries amplify the load to ~800 RPS (well above the 266 RPS capacity). Due to the large queue (10k), requests sit in the backlog, time out, and retry, creating a self-sustaining failure loop that persists indefinitely.

### 3. Static Global Budget (`ep3`)
**Config**: `ep2` + `global_retry_budget: { target_rps: 30 }`.
**Observation**: The budget prevents the retry storm. During the spike, only a small fraction of retries are allowed. The system survives the spike and recovers.

### 4. Dynamic (AIMD) Global Budget (`ep4`)
**Config**: `ep2` + `aimd_global_retry_budget`.
**Observation**: The AIMD budget detects the high failure rate and throttles retries, preserving system stability.
