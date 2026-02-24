# Online Boutique External Clients (CLIENT_HOST)

These clients run on `CLIENT_HOST` from `prototype/k8s-config.sh` (outside Kubernetes).

Purpose:
- controllable traffic behavior
- multiple heterogeneous client profiles
- retry-focused experiments

## Important note on "AWS SDK retries"

The Online Boutique frontend is a generic HTTP app, not an AWS API. AWS SDKs (e.g. boto3)
expect AWS service protocols and cannot be directly used to call storefront endpoints like
`GET /product/...`.

This folder therefore uses **AWS-SDK-style retry profiles** (good/bad/sdk-a/sdk-b/sdk-c/sdk-d)
for comparative retry-behavior experiments against the Online Boutique gateway.

If you need literal AWS SDK retry middleware, the target must be an AWS API-compatible endpoint.

## Files

- `run-clients.sh`: start/stop/status/logs for client processes on `CLIENT_HOST`
- `traffic_gen.py`: async external client runner (HTTP to gateway, Python stdlib only)
- `profiles/*.json`: split retry/timeout/traffic profiles

## Profiles

By default, `run-clients.sh start` runs **all** profile JSON files in `profiles/`:

- `good`
- `bad`
- `none`
- `sdk-a`
- `sdk-b`
- `sdk-c`
- `sdk-d`

To run only a subset, set `PROFILES` (comma-separated):

```bash
PROFILES=good,bad ./run-clients.sh start
PROFILES=sdk-a,sdk-b,sdk-c,sdk-d ./run-clients.sh start
```

## Usage

```bash
cd prototype/clients/online-boutique

# Start all configured profiles on CLIENT_HOST
./run-clients.sh start

# Check process status on CLIENT_HOST
./run-clients.sh status

# Tail logs
./run-clients.sh logs

# Fetch CSV metrics from CLIENT_HOST to local outputs/
./run-clients.sh fetch-metrics

# Stop
./run-clients.sh stop
```

By default traffic targets the online-boutique Gateway (`NodePort`) and sets
`Host: boutique.example.com`.

## Runtime behavior

- Runs on `CLIENT_HOST` using system `python3` (no `pip`, no `venv`, no external dependencies).
- `run-clients.sh` resolves the current online-boutique Gateway `NodePort` from `MASTER_HOST`.
- Client logs include JSON `startup`, `attempt`, and `request_done` events.
- Remote CSV metrics are written to:
  - `/tmp/online-boutique-clients/metrics/client_attempts.csv`
- `fetch-metrics` copies those files to:
  - `prototype/clients/online-boutique/outputs/<timestamp>/`

## Telemetry segmentation

Each request includes headers for segmentation/debugging:

- `X-Retry-Client-Type`
- `X-Retry-Client-Worker`
- `X-Request-ID`
- `X-Attempt-Number`
