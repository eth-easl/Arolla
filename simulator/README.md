# Global Retry Budget Simulator

This is a simulator for the global retry budget policy.

## Quick Start
```bash
# Complete workflow (experiment + plots)
python bin/workflow.py experiments/yaml/default.yaml

# Output structure:
results/default_20251209_084304/
├── output.csv          # Simulation data
└── plots/              # All 5 visualizations
    ├── latency.png
    ├── qps.png
    ├── queue.png
    ├── failures.png
    └── success_rate.png
```

## Policy design notes
Global retry budget:
- resilience gains
- performance gains

### 1. Operational Simplicity (The "One Knob" Rule)
In a system with 1,000 different clients (microservices, mobile apps, scripts), configuring **Client-Side Budgets** is a nightmare.
- You have to update 1,000 config files.
- "Is 10% correct for the Mobile App? Is 20% correct for the Backend?"
- **Global Server Budget**: You set **one number** on the Server: "I can handle 100 retries/sec." You ignore the clients. This drastically reduces operational toil and complexity.

### 2. Protection from "Rogue Clients"
Client-side budgets rely on the client being *well-behaved*.
- **Scenario**: A bug in "Service A (v2.0)" causes it to ignore its local budget and retry infinitely.
- **Result**: Without server-side protection, Service A takes down the Database.
- **With Global Budget**: The Server enforces the limit. It doesn't care *why* Service A is retrying; it just blocks the excess. It is the final line of defense against client-side bugs.

### Cycle Breaking
- **Scenario**: Microservice architectures with cyclic dependencies ($A \to B \to \dots \to A$) can cause "Retry Storms".
- **Benefit**: A Global Budget System can detect the correlation between retry rates and clamp budgets for both simultaneously to 0, breaking the loop instantly.

### Global Fairness (QoS)
- **Scenario**: Data center-wide load shedding events.
- **Benefit**: Implement Tiered Shedding to prioritize aggregate system survival:
  - **Tier 1 (Checkout, Login)**: 100% Budget.
  - **Tier 2 (Search, Recommendations)**: 50% Budget.
  - **Tier 3 (Logs, Analytics)**: 0% Budget.

## Capacity Planning
To understand your service's limits and calculate **Headroom**, use Little's Law.

### 1. Calculate Theoretical Capacity
The maximum throughput ($RPS_{max}$) is determined by your concurrency limit (workers) and average service time.

$$
RPS_{max} = \frac{\text{Workers}}{\text{Mean Latency (seconds)}}
$$

For **Lognormal Latency** (configured with `median_ms` and `lognorm_sigma` ($\sigma$)):
$$
\text{Mean Latency} = \text{median} \times e^{(\sigma^2 / 2)}
$$

**Example**:
*   `workers: 10`
*   `median_ms: 100` (0.1s)
*   `lognorm_sigma: 0.5`

$$
\text{Mean Latency} = 0.1 \times e^{(0.5^2 / 2)} = 0.1 \times e^{0.125} \approx 0.1 \times 1.133 = 0.1133 \text{ s}
$$

$$
RPS_{max} = \frac{10}{0.1133} \approx 88 \text{ RPS}
$$

### 2. Calculate Headroom
Headroom is the spare capacity available before queuing begins.

$$
\text{Headroom} = RPS_{max} - \text{Current Load}
$$

If your current load is **50 RPS**, your headroom is:
$$
88 - 50 = 38 \text{ RPS}
$$