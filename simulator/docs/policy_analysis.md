# Retry Policy Analysis & Results Interpretation

This document explains the mechanics of the deployed retry strategies and interprets the simulation results comparing their effectiveness under failure conditions.

## 1. Retry Policy Mechanisms

### A. Unbounded Retries (Baseline)
*   **Mechanism**: Every failed request is retried up to `max_attempts` times immediately.
*   **Behavior**: In a partial failure scenario (e.g., 50% drop), clients aggressively retry. If the service is overloaded, these retries add valid work to the queue, displacing new requests.
*   **Risk**: Susceptible to **Retry Storms** (Metastable Failure). Verification showed **Amplification > 2.4x**, meaning the system did more than double the necessary work, mostly on requests that eventually failed.

### B. Static Global Retry Budget (Token Bucket)
*   **Mechanism**: A server-side Rate Limiter for retries.
    *   **Logic**: A **Token Bucket** algorithm controls admission.
    *   **`target_rps` (Refill Rate)**: The steady-state rate of allowed retries (e.g., `40 RPS`). This determines the long-term retry capacity.
    *   **`max_burst` (Bucket Size)**: The maximum number of tokens that can accumulate. This determines how large a sudden spike of retries can be accepted before throttling begins.
    *   **Behavior**: If `tokens > 0`, retry is allowed and 1 token is consumed. Else, the retry is rejected immediately.
*   **Pros**: Low overhead, simple to understand.
*   **Cons**: **Extremely sensitive to configuration**.
    *   **Too Loose (`rps=300`)**: Provided no protection; system collapsed similar to Baseline.
    *   **Too Tight**: Rejects valid retries during minor blips.
    *   **Perfect (`rps=40`)**: Worked perfectly in our sim, but required knowing the exact "spare capacity" of the system (Capacity 200 - Load 160 = 40). In production, capacity fluctuates, making this hard to maintain.

### C. AIMD Adaptive Budget (Self-Tuning)
    *   **Logic**: Adjusts `target_rps` based on a health feedback loop.
    *   **`min_rps` (Floor)**: The minimum safe budget (e.g., `5 RPS`) to prevent complete starvation.
    *   **`additive_step` (Probing)**: If `failure_rate < threshold`, increase budget by `5 RPS/sec`. This "probes" for available capacity.
    *   **`decrease_factor` (Backoff)**: If `failure_rate > threshold`, multiply budget by `0.5`. This causes a rapid, exponential reduction in load during outages.
    *   **Benefit**: **Zero Tuning**. It found the ~40 RPS sweet spot automatically during the storm and recovered immediately when the storm ended.

---

## 2. Results Interpretation

We evaluated the policies using four key metrics.

![Comparison Dashboard](comparison_summary.png)

### Key Metrics Explained

1.  **Global Success Rate (Efficiency)** _(Higher is Better)_
    *   **Definition**: $\%$ of total user requests that eventually succeeded.
    *   **Result**: 
        *   **Baseline**: ~55% (Nearly half of all users failed).
        *   **AIMD / Static-Opt**: ~92% (Most users succeeded, even despite the 50% failure injection).
    *   **Takeaway**: An effective policy preserves user experience during partial outages.

2.  **Retry Success Rate** _(Higher is Better)_
    *   **Definition**: $\%$ of *retries* that succeeded.
    *   **Result**:
        *   **Baseline**: **3.6%**. The system spammed retries that almost all failed. This is "Goodput waste."
        *   **AIMD**: **73.3%**. It only allowed retries when they were likely to win. 3 out of 4 retries helped a user.

3.  **Amplification** _(Lower is Better)_
    *   **Definition**: Ratio of `Total Work` to `Original Requests`. An amplification of `1.0x` is ideal (no overhead).
    *   **Result**:
        *   **Baseline**: **2.43x**. The system processed 143% extra load (garbage retries), suffocating itself.
        *   **AIMD**: **1.02x**. Almost zero overhead. It stopped the storm before it started.

4.  **Final P99 Latency** _(Lower is Better)_
    *   **Result**:
        *   **Baseline**: **767ms** (Saturation). Queues were full, so requests waited seconds before timing out.
        *   **AIMD**: **215ms** (Healthy). By rejecting excess retries early, queues remained empty, ensuring fast processing for admitted requests.

### Conclusion

The **AIMD Adaptive Budget** is superior because it achieves the reliability of a perfectly tuned static budget without the operational risk of manual configuration. It effectively converts a potentially catastrophic "Retry Storm" into a minor, manageable degradation in availability.

## Appendix: Theoretical Ideal Configuration

Why was `target_rps: 40` the optimal static configuration? We can derive this from Little's Law and System Capacity.

### 1. Calculate System Capacity
*   **Workers**: 16
*   **Service Time ($T_s$)**: 60ms (0.060s)
*   **Max Throughput ($X_{max}$)**:
    $$ X_{max} = \frac{\text{Workers}}{T_s} = \frac{16}{0.060} \approx 266 \text{ RPS} $$

### 2. Calculate Spare Capacity
*   **Incoming Load ($L$)**: 188 RPS
*   **Spare Capacity ($C_{spare}$)**:
    $$ C_{spare} = X_{max} - L = 266 - 188 = 78 \text{ RPS} $$

### 3. Determine Safe Retry Budget (Refill Rate)
The system can theoretically handle 78 retries per second before saturation. However, operating at 100% capacity is dangerous due to variance. A standard reliability factor is 50%.

*   **Optimal Refill Rate**: $78 \text{ RPS} \times 0.5 \approx \mathbf{39 \text{ RPS}}$. (We used 40).

### 4. Determine Safe Max Burst (Bucket Size)
The `max_burst` determines how many retries can go through *instantly*. If this burst is too large, it creates a queue backlog that causes timeouts for everyone.
We calculate the maximum burst that can be processed within the timeout budget.

*   **Formula**: $B_{max} < \frac{\text{Timeout} \times \text{Workers}}{\text{Service Time}}$
*   **Calculation**:
    $$ B_{max} < \frac{192ms \times 16}{60ms} \approx 51.2 \text{ tokens} $$

If we allow a burst of 100 (as in the failed config), the latency impact is:
$$ L_{impact} = \frac{100 \times 60ms}{16} = 375ms $$
Since $375ms > 192ms$ (Timeout), a burst of 100 **guarantees** timeouts.

A safe burst should be well below the limit (e.g., 20%).
*   **Optimal Burst**: $51.2 \times 0.2 \approx \mathbf{10}$.

**Conclusion**:
*   **Refill Rate (40)** matches the spare capacity margin.
*   **Burst (10)** ensures even a full sudden dump of retries adds only ~37ms of latency, keeping the system healthy.
*   **The Bad Config (300 RPS, 100 Burst)** failed because 300 > 78 (Capacity) and 100 > 51 (Latency Limit).
