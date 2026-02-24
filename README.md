# Global Retry Budget Research Repo

This repository contains:

- a discrete-event simulator for retry behavior and retry budgets in microservices
- a calibration pipeline that fits simulator parameters from a live Istio deployment
- Kubernetes/Istio prototype deployments (including `online-boutique`) for experiments

## Repository Layout

- `simulator/`
  - Core retry-budget simulator (event-driven, queueing + faults + retries + policies)
  - Main docs: `simulator/README.md`
- `calibration/`
  - Collect Envoy/Istio stats from a live `online-boutique` deployment and generate calibrated simulator configs
  - Main docs: `calibration/README.md`
- `prototype/`
  - Kubernetes + Istio deployment scripts and app manifests used for experiments
  - Includes `online-boutique` and external traffic clients on `CLIENT_HOST`
- `outputs/`
  - Local experiment outputs / generated artifacts

## Most Common Workflows

### 1. Run the simulator

```bash
cd simulator
pip install -e .
python bin/workflow.py experiments/yaml/default.yaml
```

### 2. Deploy the prototype cluster app (online-boutique)

```bash
cd prototype
./deploy-app.sh online-boutique
./deploy-app.sh online-boutique --test
```

`online-boutique` fault injection is optional:

```bash
./deploy-app.sh online-boutique --fault-injection
./deploy-app.sh online-boutique --no-fault-injection
```

### 3. Run external retry-study clients on `CLIENT_HOST`

These clients run outside Kubernetes (on `CLIENT_HOST` from `prototype/k8s-config.sh`) and target the online-boutique Gateway.

```bash
cd prototype/clients/online-boutique
./run-clients.sh start
./run-clients.sh status
./run-clients.sh logs
./run-clients.sh fetch-metrics
./run-clients.sh stop
```

Run only selected profiles:

```bash
PROFILES=good,bad ./run-clients.sh start
```

### 4. Calibrate simulator from the live prototype

```bash
cd calibration
./pipeline.sh
RUN_SIM=1 COMPARE=1 ./pipeline.sh
```

This produces:
- fitted parameters (`data/fitted_params.json`)
- generated simulator config (`configs/online_boutique.yaml`)
- comparison report/plots (`reports/`)

## Notes

- The simulator and calibration pipeline are the main analysis workflow.
- The prototype `online-boutique` app disables the stock in-cluster loadgenerator by default in this repo and uses external controllable clients instead.
- External client profiles are "AWS-SDK-style" retry behaviors (not literal AWS SDK calls), because `online-boutique` is a generic HTTP application.

## Entry Points

- Simulator workflow: `simulator/bin/workflow.py`
- Simulator single run: `simulator/bin/run_experiment.py`
- Calibration pipeline: `calibration/pipeline.sh`
- Prototype deployer: `prototype/deploy-app.sh`
- External clients (online-boutique): `prototype/clients/online-boutique/run-clients.sh`
