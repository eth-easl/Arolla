# Client profiles

Each JSON file in this directory defines one *client type*. At runtime
`traffic_gen.py` loads all `*.json` files and spawns load generators
according to each profile's `mode`. There are two load-generation modes:

| Mode | What it models | When to use |
|---|---|---|
| **`closed-loop`** (default) | N patient users, each waits for response before next | Functional tests, latency measurement, capacity finding without overload risk |
| **`open-loop`** | Fixed-rate firing schedule, fire-and-forget, in-flight grows under overload | Metastability experiments, real production traffic semantics, overload testing |

The two modes share the same request mix, retry policy, and CSV output schema.
The differences are entirely in *how* requests are dispatched.

## Closed-loop schema

The default. `count` worker tasks, each owning one persistent HTTP/1.1
connection, each looping `pick → execute → sleep` to target the per-worker rate.

```json
{
  "name": "checkout",
  "description": "Human-readable description, optional",
  "mode": "closed-loop",          // optional; closed-loop is the default

  "count": 3,                     // concurrent workers (= TCP connections to gateway)
  "rate_rps_per_client": 0.5,     // per-worker target send rate
  "timeout_s": 3.0,               // per-HTTP-attempt timeout

  "sku_pool": ["OLJCESPC7Z", "..."],     // {sku} in paths/bodies resolves to
                                          // one of these (per top-level pick)

  "requests": [ /* weighted request mix — see below */ ],

  "retries": 3,
  "backoff": { "mode": "exponential", "base_s": 0.1, "max_s": 2.0, "jitter": "full" },
  "retry_on_status": [0, 500, 502, 503, 504]
}
```

**Properties of closed-loop:**

- Maximum offered RPS = `count × rate_rps_per_client` *only when the system
  is fast enough*. Under overload, per-request latency grows, workers stall,
  and the actual offered rate drops.
- In-flight request count is bounded by `count`. The client cannot DoS the
  backend by piling on more concurrent requests than `count`.
- Each worker holds one TCP connection for its lifetime. `count` is also the
  total number of TCP connections to the gateway.

## Open-loop schema

A single firer coroutine fires requests on a wall-clock schedule. Requests
are fire-and-forget: the firer schedules the next request based on
`rate_rps`, regardless of whether previous requests have completed. A
bounded `pool_size` of HTTP connections is shared by all in-flight tasks.

```json
{
  "name": "stress",
  "mode": "open-loop",            // required for open-loop mode

  "rate_rps": 200,                // target firing rate (requests/sec)
  "pool_size": 60,                // shared HTTP connection pool size
  "max_inflight": 5000,           // hard cap on concurrent in-flight (0 = unbounded)
  "timeout_s": 3.0,               // per-HTTP-attempt timeout

  "sku_pool": [...],
  "requests": [ /* same as closed-loop */ ],

  "retries": 3,
  "backoff": { "mode": "none" },
  "retry_on_status": [0, 500, 502, 503, 504]
}
```

**Properties of open-loop:**

- Offered RPS is **exactly `rate_rps`**, regardless of backend latency. The
  firer schedules the next fire based on wall-clock, not on request completion.
- In-flight count grows when the backend is slow. By Little's Law,
  `in-flight ≈ rate_rps × per_request_latency`. At 200 rps and 2-second
  latency, that's 400 concurrent in-flight requests.
- **`pool_size` bounds the number of TCP connections** to the gateway, NOT
  the number of in-flight requests. If all sessions are checked out when
  the firer dispatches a new request, the new request waits for one to
  return. This adds queueing delay at the client but doesn't drop requests.
- **`max_inflight` is a safety valve**: when in-flight count reaches this,
  new fires are dropped immediately and logged as `status: -1` rows in the
  CSV (`request_type: client-overload`). Set to 0 for unbounded.

**When to use open-loop:**

- Reproducing metastable failure modes (closed-loop has built-in
  back-pressure that prevents the metastable trap)
- Modeling real production traffic where users don't wait politely
- Capacity tests where you want to know "what happens if we offer 10× capacity?"

**When NOT to use open-loop:**

- The gateway has a tight connection-pool limit you haven't raised. Open-loop
  will hit the gateway's pool before the backend, and you'll measure the
  gateway's failure mode instead of the backend's. Either scale the gateway
  first or stay with closed-loop.
- You want predictable, repeatable load. Open-loop's in-flight depth depends
  on backend latency, which can vary between runs.

## Shared schema (both modes)

These fields apply identically to closed-loop and open-loop:

### Weights

Weights are *relative*. The three entries `[40, 40, 20]` produce the same
distribution as `[2, 2, 1]` or `[0.4, 0.4, 0.2]`. Values visible in structured
log output are normalized to percentages so you can see the real mix.

### `{sku}` substitution

Any occurrence of `{sku}` in a `path`, `body`, or workflow step's `path`/`body`
is replaced with a random SKU from `sku_pool` per top-level pick. Within a
workflow, the same SKU is reused across every step (so you add product X and
then check out product X in the same logical transaction).

### Workflows

A request entry with a `workflow` key is a multi-step macro. All steps share
a single session UUID (passed as `Cookie: shop_session-id=<uuid>`), so
Online Boutique sees them as the same user's session. Each step gets its own
retry attempts and its own CSV row. If any step fails after exhausting
retries, the rest of the workflow is skipped (you can't check out without a
successful add-to-cart).

### Retry behavior

Retry policy is *per profile*, applied uniformly to every request and every
workflow step that the profile sends. If you need different retry behavior
for different request types, make it a separate profile. Each additional
attempt increments the `X-Attempt-Number` request header — the Arolla filter
reads this to distinguish first attempts from retries.

### `timeout_s` semantics

`timeout_s` is **per-attempt, not per-logical-request**. It's passed straight
through to `http.client.HTTPConnection(timeout=…)` and applies to each
individual socket operation (connect, send, recv) within one HTTP attempt.

A logical request that exhausts all retries can therefore consume up to
`(retries + 1) × timeout_s` of wall-clock time. For example, with
`timeout_s: 1.0` and `retries: 5`, a failing request can take up to **6
seconds** before the worker gives up and loops back to the top.

There is **no per-logical-request cap** — if you need one, either reduce
`retries` or shrink `timeout_s`.

Network-level timeouts show up in the CSV as `status: 0`. Add `0` to
`retry_on_status` if you want those to count as retryable failures; leave it
out if you want the request to fail fast on connection problems.

### Connection reuse

`traffic_gen.py` maintains **one persistent HTTP/1.1 connection per worker**
via `http.client.HTTPConnection` with `Connection: keep-alive`. If the
connection breaks (server closes, socket error, timeout), the worker
transparently reconnects on the next attempt. This means under steady
traffic, each worker's TCP connection count is **1**, not "one per request".
TCP handshake cost is paid once at startup, not every call.

If you suspect keep-alive isn't working (e.g. seeing many `status: 0` under
light load), check for server-side connection limits or `Connection: close`
responses from a fronting proxy.

### Session cookie

Every HTTP request carries a `Cookie: shop_session-id=<uuid>` header. A fresh
UUID is generated for each top-level pick from the `requests` array:

- Single request: the UUID is used for just that one request.
- Workflow: the UUID is reused across all steps in the workflow.

This keeps non-workflow requests stateless (each starts with an empty cart)
while letting workflows maintain cart state across their steps.

## CSV output schema

One row per HTTP attempt:

```
timestamp, profile, worker, request_id, request_type, method, path,
attempt, is_retry, status, ok, latency_s
```

- `request_type` matches the `name` of the selected request (or workflow step).
- `attempt` starts at 1 and increments on retries.
- `is_retry` is 1 for `attempt > 1`, 0 otherwise.
- `ok` is 1 when `200 <= status < 400`.

## Profile library (current)

| Profile                  | Mode       | Purpose |
|--------------------------|------------|---------|
| `post-cart-stress-open`  | open-loop  | Cart-focused stress for cartservice fault scenarios (canonical 25 + multi-spike) |
| `browse-stress-open`     | open-loop  | Browse workload for productcatalogservice alt-service scenarios (AS1) |
| `checkout-stress-open`   | open-loop  | Checkout workflow for paymentservice alt-service scenarios (AS2) |

`run-experiment.sh` and `run_full_sweep.sh` pass `--client-profiles` explicitly so only
the profile for each scenario is loaded.

All three profiles use open-loop firing with per-scenario `rate_rps` tuned by
`run_full_sweep.sh` before each run.

### Subsetting for a specific experiment

To run just a few profiles, set the `PROFILES` env var when invoking
`run-clients.sh`:

```bash
PROFILES=browse,checkout \
    prototype/clients/online-boutique/run-clients.sh start
```

Or pass `--profiles browse,checkout` to `traffic_gen.py` directly.
