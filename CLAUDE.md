# Project: Global Retry Budget Simulator

Research project studying retry amplification and global retry budgets in microservice architectures. Combines a discrete-event simulator with live Kubernetes/Istio prototype experiments.

## Repository Layout

- `simulator/` — Python discrete-event simulator (M/G/c/K queues, retry policies, AIMD budgets)
- `calibration/` — Pipeline to extract parameters from live Istio clusters and validate simulator accuracy
- `prototype/` — Kubernetes/Istio deployment scripts and manifests (Emulab infrastructure)
- `outputs/` — Experiment results (CSV metrics, plots)

## Development Setup

- Python 3.10+, virtual env at `.venv/`
- Install: `cd simulator && pip install -e .`
- Run tests: `cd simulator && python -m pytest tests/ -v`
- Formatters: `black`, linter: `ruff` / `flake8`, type checker: `mypy`

## Running Experiments

- Single experiment: `python simulator/bin/workflow.py simulator/experiments/yaml/<config>.yaml`
- Parameter sweep: `python simulator/bin/run_sweep.py simulator/experiments/yaml/<config>.yaml`
- Batch run: `python simulator/bin/run_batch.py simulator/experiments/yaml/<dir>/`
- Calibration pipeline: `cd calibration && ./pipeline.sh --wait 60 --duration 120`

## Experiment Configs

Experiments are defined in YAML under `simulator/experiments/yaml/`. Configs specify:
- Service topology (latency distributions, workers, queue capacity)
- Retry policies (fixed, exponential, jittered backoff)
- Load limiters (token bucket, rate limiting)
- Retry budgets (static token bucket or AIMD self-tuning)
- Fault injection (partial failures, latency spikes)
- Workload (Poisson arrivals, load spikes)

## Code Conventions

- Config schemas use Pydantic v2 (`simulator/src/simulator/config/schema.py`)
- Policies are composable via middleware chain (`simulator/src/simulator/middleware/`)
- Commit messages follow conventional commits: `feat()`, `fix()`, `chore()`, `refactor()`
- This is a research prototype — prioritize correctness and clarity over production polish

## Important Context

- This is active research: multiple experiment branches exist (dev/*, prototype/*, simulator/*)
- The simulator validates against real Kubernetes/Istio deployments on Emulab
- Calibration uses lognormal fits from Envoy sidecar stats
- Do NOT delete or overwrite files in `outputs/` or `calibration/data/` without asking — these may contain hard-to-reproduce experimental results
- When modifying simulator core (engine, runtime, policies), always run the test suite afterward
