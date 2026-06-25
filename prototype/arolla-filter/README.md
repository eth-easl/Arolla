# Arolla Envoy filter (Proxy-Wasm)

Server-side retry admission filter implementing the paper's Algorithm 1 as an
Envoy HTTP filter, deployable in an Istio service mesh via the `WasmPlugin` CRD.

**MVP scope**: aggregate mixed-refill token bucket only. Per-tenant max-min
fairness (paper §4.3) is not yet implemented — enough to run §6.2.1, §6.2.2,
§6.2.3, §6.5 sensitivity, and §6.6 overhead experiments.

## Algorithm

Mixed-refill token bucket per upstream cluster:

- `r` — tokens deposited per successful response (event refill)
- `t` — tokens added per second regardless of traffic (time refill)
- `C` — bucket capacity (cap)
- Retries consume one token; first attempts bypass admission entirely.
- On reject, the filter sends a local `429` with `x-arolla-rejected: 1`.

Defaults (paper §5): `r=0.1, C=10, t=1.0`.

## Retry identification

The filter reads the `x-attempt-number` HTTP header on each request:

| Header value | Meaning            | Behavior               |
|--------------|--------------------|------------------------|
| missing / 1  | first attempt      | always admit           |
| ≥ 2          | retry              | subject to admission   |

The load generator in `../clients/online-boutique/traffic_gen.py` already sets
this header. For internal hops (frontend → checkout → cart → …), the calling
service's local sidecar needs to propagate and increment the counter; a future
iteration will add an outbound filter that does this automatically. For now,
the filter only gates external client retries observed at the sidecar where
it is installed.

## Build

One-time setup on the build host:

    cd prototype/arolla-filter
    make install-target     # installs the wasm32-wasip1 rustup target

Then:

    make build

Produces `target/wasm32-wasip1/release/arolla_filter.wasm` (≈100 KB after
release + LTO + strip).

## Deploy into the Istio mesh

The filter can be loaded three ways by a `WasmPlugin` CRD — pick whichever fits
your cluster:

### Option A: HTTP server (simplest for Emulab one-cluster testbed)

On the master node (any machine reachable by cluster nodes):

    # Serve the wasm binary over plain HTTP
    cd prototype/arolla-filter/target/wasm32-wasip1/release
    python3 -m http.server 8000

Then in [../manifests/online-boutique/policies/arolla.yaml](../manifests/online-boutique/policies/arolla.yaml),
set:

    url: http://<MASTER_IP>:8000/arolla_filter.wasm

### Option B: OCI registry

If you have a reachable registry (e.g. a local Docker registry on the master):

    oras push <registry>/arolla:latest \
        target/wasm32-wasip1/release/arolla_filter.wasm:application/vnd.module.wasm.content.layer.v1+wasm

Then set `url: oci://<registry>/arolla:latest` in the `WasmPlugin`.

### Option C: ConfigMap + user volume mount

Base64-encode into a ConfigMap, mount into every istio-proxy via
`sidecar.istio.io/userVolume` / `userVolumeMount` annotations, and reference
via `url: file:///etc/wasm/arolla_filter.wasm`. More setup; see the [Istio
Wasm docs](https://istio.io/latest/docs/reference/config/proxy_extensions/wasm-plugin/).

## Tuning the token-bucket parameters

`WasmPlugin.spec.pluginConfig` accepts a JSON object:

```yaml
pluginConfig:
  r: 0.1              # event refill per success
  capacity: 10.0      # bucket cap (C)
  t: 1.0              # time refill per second
  attempt_header: "x-attempt-number"
  reject_status: 429
```

A configuration update pushes via xDS without restarting the proxy.

## Observability

The filter exports four Envoy stats:

- `arolla_retries_admitted_total` (counter)
- `arolla_retries_rejected_total` (counter)
- `arolla_first_attempts_total` (counter)
- `arolla_bucket_tokens` (gauge — current token count)

Scrape via Istio's Prometheus:

    kubectl -n online-boutique exec <pod> -c istio-proxy -- \
        curl -s localhost:15000/stats | grep arolla_

## Smoke test

With Online Boutique deployed and a fault injected via
[../manifests/online-boutique/faults/cartservice-50pct.yaml](../manifests/online-boutique/faults/cartservice-50pct.yaml):

    kubectl apply -f ../manifests/online-boutique/policies/arolla.yaml
    kubectl apply -f ../manifests/online-boutique/faults/cartservice-50pct.yaml
    # ... drive load via clients/online-boutique/run-clients.sh start ...
    kubectl apply -f ../manifests/online-boutique/policies/arolla.yaml    # check stats
    kubectl -n online-boutique exec deploy/cartservice -c istio-proxy -- \
        curl -s localhost:15000/stats | grep arolla_
    # expect: arolla_retries_rejected_total > 0 once bucket drains

## Known limitations (MVP)

- **No per-tenant fairness** (§6.4 experiment blocked until that layer
  lands).
- **gRPC status trailers are not inspected.** Online Boutique's
  service-to-service calls use gRPC, which always returns HTTP `:status 200`
  and encodes errors in the `grpc-status` trailer. The filter currently counts
  all 200s as successes, which over-credits the bucket on gRPC errors. A
  future iteration should inspect `on_http_response_trailers()`.
- **Retry counter propagation is client-side only.** Internal hops do not
  increment `x-attempt-number`, so the filter effectively only gates retries
  originating from the external load generator. Paper §5 says the local proxy
  should set/increment this — a future outbound filter will close this gap.
- **First-come-first-served within the aggregate bucket.** Without the
  per-tenant layer, one aggressive caller can still monopolize the bucket.
