# Client profiles

Each JSON file in this directory defines one *client type*. At runtime
`traffic_gen.py` loads all `*.json` files, spawns `count` workers per profile,
and has each worker loop forever: weighted-pick a request, execute it (with
retries per the profile's policy), sleep to hit the target RPS, repeat.

## Schema

```json
{
  "name": "checkout",
  "description": "Human-readable description, optional",

  "count": 3,                     // concurrent workers of this profile
  "rate_rps_per_client": 0.5,     // per-worker send rate
  "timeout_s": 3.0,               // per-attempt HTTP timeout

  "sku_pool": ["OLJCESPC7Z", "..."],     // {sku} in paths/bodies resolves to
                                          // one of these (per top-level pick)

  "requests": [                   // weighted request mix
    {
      "name": "home",              // used in the CSV request_type column
      "weight": 15,                 // relative weight; sums don't need to be 100
      "method": "GET",
      "path": "/"
    },
    {
      "name": "add-to-cart",
      "weight": 20,
      "method": "POST",
      "path": "/cart",
      "body": "product_id={sku}&quantity=1",
      "content_type": "application/x-www-form-urlencoded"
    },
    {
      "name": "checkout-flow",
      "weight": 20,
      "workflow": [               // multi-step, sharing one session cookie
        { "name": "checkout-add",   "method": "POST", "path": "/cart",
          "body": "product_id={sku}&quantity=1" },
        { "name": "checkout-place", "method": "POST", "path": "/cart/checkout",
          "body": "email=..." }
      ]
    }
  ],

  "retries": 3,                                    // max retries per step
  "backoff": {                                     // applied between retries
    "mode": "exponential",                          // "none" | "fixed" | "exponential"
    "base_s": 0.1,
    "max_s": 2.0,
    "jitter": "full"                                // "none" | "full"
  },
  "retry_on_status": [500, 502, 503, 504]         // 0 = network error, always retryable
}
```

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

| Profile         | Workers × RPS | Root rps | Retries | Backoff          | Purpose                                           |
|-----------------|---------------|---------:|---------|------------------|---------------------------------------------------|
| `browse`        | 4 × 1.0       |      4.0 | 2       | exp 0.1→2s + jit | Read-only catalog browser (no POSTs, no checkout) |
| `checkout`      | 3 × 0.5       |      1.5 | 3       | exp 0.1→2s + jit | Full shopper: browse + cart + checkout workflow   |
| `conservative`  | 3 × 0.5       |      1.5 | 1       | fixed 500ms      | Well-behaved tenant for §6.4 fairness             |
| `aggressive`    | 20 × 2.0      |     40.0 | 5       | none (0ms)       | Misbehaving tenant (§6.4) — mixed request workload|
| `cart-stress`   | **20 × 2.0**  | **40.0** | 5       | none (0ms)       | **Cart-focused** aggressive retry — 100% POST /cart; use this for cart-service fault smoke tests |
| `no-retry`      | 2 × 1.0       |      2.0 | 0       | —                | Control — raw server-visible failure rate         |

**Total steady-state load** (all profiles combined): ~49 root rps.
Under retry amplification during a fault, total HTTP call rate can reach
~150-250 req/s — driven almost entirely by the `aggressive` profile.

## Scaling load

Two knobs, each tuned per profile:

- **`count`** — number of concurrent worker tasks. More workers → more
  parallelism, bounded by the traffic_gen.py thread pool (`DEFAULT_HTTP_THREAD_POOL_SIZE = 256`).
  Bump this first when you want more load.
- **`rate_rps_per_client`** — target per-worker request rate. Workers sleep
  `1 / rate_rps_per_client` seconds between requests. Under fault with long
  retry chains a worker can easily exceed its interval (each attempt is up
  to `timeout_s` long), in which case it goes back-to-back — so this knob
  mostly controls the *pre-fault* steady-state rate, not the peak.

**To push harder**: bump `count` in the profile(s) you want to stress.
`traffic_gen.py` sizes its thread pool to 256, so `count: 100` per profile
is fine on a single CLIENT_HOST. Beyond that you'll need a second load
generator node.

**To model a congested-pool client**: keep `count` low but set very short
`timeout_s` and high `retries`. This models the "stuck in retry" failure
mode without generating huge root rates.

All profiles other than `browse` use the same 15/25/20/20/20 request mix so
their failure modes can be compared on the same code paths. `browse` uses
40/40/20 GET-only to avoid cluttering the cart-oriented experiments with
noise from read-only clients.

### Subsetting for a specific experiment

To run just a few profiles, set the `PROFILES` env var when invoking
`run-clients.sh`:

```bash
PROFILES=browse,checkout \
    prototype/clients/online-boutique/run-clients.sh start
```

Or pass `--profiles browse,checkout` to `traffic_gen.py` directly.
