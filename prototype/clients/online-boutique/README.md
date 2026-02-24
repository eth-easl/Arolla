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
modeled after the retry profile study in `aws_outage/client/traffic_gen.py`.

If you need literal AWS SDK retry middleware, the target must be an AWS API-compatible endpoint.

## Files

- `run-clients.sh`: start/stop/status/logs for client processes on `CLIENT_HOST`
- `traffic_gen.py`: async external client runner (HTTP to gateway)
- `profiles/*.json`: split retry/timeout/traffic profiles

## Usage

```bash
cd prototype/clients/online-boutique

# Start all configured profiles on CLIENT_HOST
./run-clients.sh start

# Check process status on CLIENT_HOST
./run-clients.sh status

# Tail logs
./run-clients.sh logs

# Stop
./run-clients.sh stop
```

By default traffic targets the online-boutique Gateway (`NodePort`) and sets
`Host: boutique.example.com`.
