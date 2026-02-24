# Calibration Pipeline

Automatically fits simulator parameters from a live `online-boutique` deployment
so the simulator reproduces the prototype's real-world latency, load, and service
topology for retry-budget analysis.

```
prototype (running cluster)
        │
   collect.sh  ──→  data/raw_stats/<service>.txt    (Envoy /stats dumps)
        │
   fit.py      ──→  data/fitted_params.json         (lognormal fit + workers + RPS)
        │
   generate_config.py ──→  configs/online_boutique.yaml  (simulator YAML)
        │
   simulator/bin/run_experiment.py  ──→  data/sim_output/  (optional)
        │
   compare.py  ──→  reports/calibration_report.txt  (sim vs prototype table)
                    reports/latency_comparison.png   (latency-over-time plot)
```

---

## Folder layout

```
calibration/
  collect.sh            # Stage 1: SSH into cluster, dump Envoy /stats
  fit.py                # Stage 2: parse histograms, fit lognormal
  generate_config.py    # Stage 3: emit simulator YAML
  compare.py            # Stage 5: compare sim vs prototype, plot
  pipeline.sh           # Orchestrator for all stages
  topology.py           # Hardcoded online-boutique service graph
  README.md

  data/
    raw_stats/          # ← Envoy stat files (one per service)
    fitted_params.json  # ← Lognormal fit results
    sim_output/         # ← Simulator output CSVs

  configs/
    online_boutique.yaml  # ← Generated simulator YAML (edit for fault injection)

  reports/
    calibration_report.txt    # ← P50/P99 comparison table
    latency_comparison.png    # ← Latency-over-time plot
```

> `data/` and `reports/` are generated; `configs/` can be checked in as a baseline.

---

## Prerequisites

1. **Cluster running** — `online-boutique` deployed and receiving traffic:
   ```bash
   cd prototype
   ./deploy-app.sh online-boutique
   ```

2. **SSH access** configured in `prototype/k8s-config.sh`
   (`MASTER_HOST`, `SSH_USER`, `SSH_KEY`, `SSH_OPTS`).

3. **Python dependencies** installed:
   ```bash
   pip install -r ../simulator/requirements.txt
   ```

---

## Quick start

```bash
cd calibration

# Collect (60s window) + fit + generate simulator YAML
./pipeline.sh

# Run the simulator with the generated YAML:
python3 ../simulator/bin/run_experiment.py configs/online_boutique.yaml \
    --output data/sim_output/ --verbose
```

---

## Full pipeline with comparison

```bash
# Collect + fit + generate + simulate + compare + plot
RUN_SIM=1 COMPARE=1 ./pipeline.sh
```

Produces `reports/calibration_report.txt` comparing simulated vs observed P50/P99
per service, and `reports/latency_comparison.png` showing simulated latency
time-series alongside the observed reference values. A P99 error below 25%
indicates a good fit.

---

## Options

### `pipeline.sh`

| Flag / Env var | Default | Description |
|----------------|---------|-------------|
| `--wait N` / `WAIT_SECS=N` | `60` | Envoy counter collection window (seconds) |
| `--duration N` / `DURATION=N` | `120` | Simulator run duration (seconds) |
| `--rps N` / `RPS=N` | auto | Override frontend base_rps |
| `--run-sim` / `RUN_SIM=1` | off | Run simulator after generating YAML |
| `--compare` / `COMPARE=1` | off | Run comparison + plot after simulation |

```bash
./pipeline.sh --wait 120                       # longer collection window
./pipeline.sh --duration 300 --rps 15          # custom duration and load
RUN_SIM=1 COMPARE=1 DURATION=300 ./pipeline.sh
```

### Running stages individually

```bash
# Stage 1: collect Envoy stats
./collect.sh --wait 60 --output data/raw_stats/

# Stage 2: fit parameters
python3 fit.py data/raw_stats/ --out data/fitted_params.json

# Stage 3: generate YAML
python3 generate_config.py data/fitted_params.json --out configs/online_boutique.yaml

# Stage 4: simulate (optional)
python3 ../simulator/bin/run_experiment.py configs/online_boutique.yaml \
    --output data/sim_output/ --verbose

# Stage 5: compare + plot (optional)
python3 compare.py data/fitted_params.json data/sim_output/service_metrics.csv \
    --report reports/calibration_report.txt \
    --plot-dir reports/
```

---

## How parameter fitting works

| Parameter | How it's derived |
|-----------|-----------------|
| `median_ms` | P50 from the Istio `istio_request_duration_milliseconds` Envoy histogram |
| `lognorm_sigma` | Least-squares fit over P25/P75/P90/P95/P99 via scipy |
| `workers` | `max(4, ceil(rps × p50/1000), rq_active) × 2` (Little's Law + 2× headroom) |
| `base_rps` | `cluster.inbound|<port>||;.upstream_rq_total` counter ÷ window (frontend) |
| `queue_capacity` | `workers × 4` |

**Why 2× worker headroom?** The simulator's M/G/c/K model starts queuing
requests the moment `in_flight == workers`. Without headroom, the simulator
would be saturated at the calibrated load, which does not reflect normal
(pre-fault) prototype behavior.

**`redis-cart` (TCP):** No HTTP histogram is available. Latency is fixed at
`1ms / sigma=0.1` (Redis is sub-millisecond in-memory).

---

## Adding fault injection for retry analysis

Edit `configs/online_boutique.yaml` to inject faults and study retry amplification:

```yaml
# Under productcatalogservice (called by frontend, checkout, recommendations)
partial_failures:
  - type: partial_failure
    start_s: 30
    end_s: 90
    p_fail: 0.5

# Optionally add a global retry budget:
global_retry_budget:
  target_rps: 20
  max_burst: 5
```

Re-run the simulator:
```bash
python3 ../simulator/bin/run_experiment.py configs/online_boutique.yaml \
    --output data/sim_output_fault/ --verbose
```

---

## Output files

| Path | Description |
|------|-------------|
| `data/raw_stats/<svc>.txt` | Raw Envoy `/stats` dump per service pod |
| `data/fitted_params.json` | Fitted parameters (median_ms, sigma, workers, rps) per service |
| `configs/online_boutique.yaml` | Simulator experiment config (edit for fault injection) |
| `data/sim_output/output.csv` | Client-side time-series metrics (p50, p99, retries, …) |
| `data/sim_output/service_metrics.csv` | Per-service time-series metrics |
| `reports/calibration_report.txt` | Sim vs prototype P50/P99 comparison table |
| `reports/latency_comparison.png` | Latency-over-time plot: sim lines vs observed reference |

---

## Service topology

Hardcoded in `topology.py` from the
[official online-boutique architecture](https://github.com/GoogleCloudPlatform/microservices-demo/tree/main):

```
frontend ──seq──→ cartservice ──→ redis-cart
               → productcatalogservice
               → currencyservice
               → shippingservice
               → checkoutservice ──seq──→ cartservice
               │                        → productcatalogservice
               │                        → shippingservice
               │                        → currencyservice
               │                        → emailservice (optional)
               │                        → paymentservice
               → recommendationservice ──→ productcatalogservice
               → adservice (optional)
```

All call patterns are `sequential` (conservative; matches worst-case
end-to-end latency for page assembly and checkout flow).
