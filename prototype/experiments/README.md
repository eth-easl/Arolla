# Prototype experiments

End-to-end orchestrator for running retry-policy experiments on the
Online Boutique prototype. One command drives the full lifecycle:

1. **Switch policy** (via [../deploy-policy.sh](../deploy-policy.sh))
2. **Start load** on the external client host (via [../clients/online-boutique/run-clients.sh](../clients/online-boutique/run-clients.sh))
3. **Wait through the phases** — warmup, pre-fault, fault, recovery, cooldown
4. **Inject and remove fault** at the configured times
5. **Collect metrics** — client CSVs + Envoy sidecar `/stats` dumps
6. **Aggregate and plot** via [analyze.py](analyze.py)

## Files

```
prototype/experiments/
├── run-experiment.sh           ← main orchestrator
├── analyze.py                  ← metrics aggregator + plotter
└── scenarios/
    ├── sustained-failure.conf  ← paper §6.2.1
    ├── recovery-overload.conf  ← paper §6.2.2
    └── transient.conf          ← paper §6.3 (single-burst approx)

outputs/prototype/runs/         ← output: one subdir per invocation
└── <timestamp>/                ← (sits at the repo root, not under prototype/)
        ├── experiment.json
        ├── summary.csv
        ├── plots/
        │   ├── goodput.pdf
        │   ├── amplification.pdf
        │   ├── retry-efficiency.pdf
        │   └── recovery-time.pdf
        ├── no-control/
        │   ├── timeline.json
        │   ├── client-metrics/*.csv
        │   ├── sidecar-stats/*.stats
        │   └── traffic_gen.log
        ├── circuit-breaker/
        ├── envoy-retry-budget/
        └── arolla/
```

## Prerequisites

Everything that [../arolla-filter/README.md](../arolla-filter/README.md) lists, plus:

- `kubectl` context pointed at the Emulab cluster
- Online Boutique deployed (`../deploy-app.sh online-boutique`)
- Arolla wasm built, uploaded, and served (`../deploy-policy.sh build-wasm upload-wasm serve-wasm`)
- `python3` with `pandas`, `numpy`, `matplotlib` (for `analyze.py`)

## Quick start

Run the paper §6.2.1 experiment across all four policies:

    prototype/experiments/run-experiment.sh

That's it — defaults match the paper: 60s warmup, 30s pre-fault, 60s fault,
60s recovery, 30s cooldown = 4 minutes per policy × 4 policies = ~16 minutes.

### See the timeline without touching the cluster

    prototype/experiments/run-experiment.sh --dry-run

Prints the per-policy schedule and the grand-total runtime estimate, then
exits.

### Test just one policy

    prototype/experiments/run-experiment.sh --policies arolla

### Override phase durations

    prototype/experiments/run-experiment.sh \
        --warmup 30 --prefault 15 --fault 90 --recovery 120

CLI flags take precedence over scenario config, which takes precedence over
the built-in defaults.

### Switch scenario

    prototype/experiments/run-experiment.sh --scenario recovery-overload

Valid scenarios are the `.conf` files under `scenarios/`. Add a new one by
dropping in a new `.conf` that sets `SCENARIO_FAULT_MANIFEST` and any phase
overrides.

### Analyze a previous run

    prototype/experiments/analyze.py outputs/prototype/runs/20260406_200000

Regenerates `summary.csv` and `plots/*.pdf` in place from the already-collected
data.

## Experiment timeline

For each policy, the orchestrator walks through this schedule with drift-free
absolute timestamps (not cumulative `sleep`):

```
        0s ────────────────── start load generator
                [warmup      ]   (default 60s)
     +60s ────────────────── baseline begins
                [pre-fault   ]   (default 30s)
     +90s ────────────────── kubectl apply <fault>
                [fault       ]   (default 60s)
    +150s ────────────────── kubectl delete <fault>
                [recovery    ]   (default 60s)
    +210s ────────────────── stop load generator
                [cooldown    ]   (default 30s)
    +240s ────────────────── collect metrics → next policy
```

Between policies, the orchestrator:

1. Deletes any lingering fault manifest
2. Clears the remote client workspace (`/tmp/online-boutique-clients/`)
3. Runs `deploy-policy.sh switch <next>` to tear down the previous policy
   and apply the new one
4. Sleeps `--settle` seconds (default 5) so the new `WasmPlugin` /
   `EnvoyFilter` / `DestinationRule` has time to propagate via xDS

## What gets collected

### Client side (primary data source)

`<policy>/client-metrics/*.csv` — every row is one request attempt, with:

    timestamp, profile, worker, request_id, path, attempt, is_retry, status, ok, latency_s

This is the end-user view: "what did the client actually observe?" The
analyzer uses these rows to compute goodput-over-time, amplification, and
retry efficiency.

### Sidecar stats (cross-check + Arolla internal counters)

`<policy>/sidecar-stats/<service>.stats` — Envoy admin `/stats` dump from one
pod per service. Includes:

- `envoy_cluster_upstream_rq_total` — per-upstream request counts
- `envoy_cluster_upstream_rq_retry` — retries Envoy saw (per upstream)
- `envoy_arolla_retries_admitted_total` — Arolla-specific
- `envoy_arolla_retries_rejected_total` — Arolla-specific
- `envoy_arolla_bucket_tokens` — gauge, current `B_agg`

Use these to sanity-check the client-side numbers and (for the Arolla run)
to verify the bucket actually drained and refilled as expected.

### Timeline

`<policy>/timeline.json` — absolute wall-clock timestamps for every phase
boundary. `analyze.py` uses these to slice the CSV into pre-fault / fault /
recovery windows.

## Metrics computed

`analyze.py` writes `summary.csv` with one row per policy:

| Column                 | Meaning                                              |
|------------------------|------------------------------------------------------|
| `amplification`        | total attempts / first attempts during fault         |
| `retry_efficiency_pct` | (retries that ended `ok`) / total retries × 100      |
| `avg_goodput_prefault` | mean successful requests/s during pre-fault baseline |
| `avg_goodput_fault`    | mean successful requests/s during fault window       |
| `recovery_sec`         | seconds from fault-end until goodput ≥ 95% pre-fault |
| `arolla_admitted`      | sum of `arolla_retries_admitted_total` (all sidecars)|
| `arolla_rejected`      | sum of `arolla_retries_rejected_total` (all sidecars)|

and renders four plots under `plots/`:

- `goodput.pdf` — goodput vs time, one line per policy, shaded fault region
- `amplification.pdf` — bar chart across policies
- `retry-efficiency.pdf` — bar chart across policies
- `recovery-time.pdf` — bar chart across policies

## Cleanup semantics

The orchestrator installs a TERM/INT trap that:

- Deletes the fault manifest (if still applied)
- Kills the remote `traffic_gen.py` PID (if still running)

So Ctrl-C at any point leaves the cluster in a clean state. If the script
crashes or is killed with SIGKILL, you may need to run:

    kubectl delete --ignore-not-found -f <fault manifest>
    prototype/clients/online-boutique/run-clients.sh stop
    prototype/deploy-policy.sh switch none

by hand.

## Known gaps

- **Recovery-overload scenario uses `faults/productcatalog-fault.yaml`**,
  which is 50% abort + 30% delay on productcatalog — not the "fully offline"
  100% outage described in paper §6.2.2. Add a new manifest for an exact
  match.
- **Transient scenario is a single-burst approximation** of the multi-burst
  pattern in §6.3. To reproduce the paper's 5-burst timeline, extend
  `run-experiment.sh` with a burst-loop mode or run this scenario repeatedly.
- **§6.2.3 chain-depth amplification** is not yet wrapped as a scenario — it
  needs per-hop retry-rate queries against Istio Prometheus, which the
  current analyzer doesn't implement.
- **§6.4 fairness** is blocked on the per-tenant layer of the Arolla filter
  (not yet implemented; see `arolla-filter/README.md` "Known limitations").
- **§6.5 sensitivity sweeps** are not yet orchestrated — they'd need a loop
  that re-templates the Arolla `pluginConfig` (varying `r`, `capacity`) and
  re-runs §6.2.1 for each value.
- **gRPC trailer handling** in the Arolla filter treats all HTTP 200s as
  success; see `arolla-filter/README.md` "Known limitations".
