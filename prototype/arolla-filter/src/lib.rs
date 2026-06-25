//! Arolla — server-side retry admission as an Envoy HTTP filter (Proxy-Wasm).
//!
//! Implements the aggregate mixed-refill token bucket from the paper's
//! Algorithm 1 (§4.2). Per-tenant max-min fairness (§4.3) is intentionally
//! out of scope for this MVP; it enforces a single aggregate budget per
//! upstream cluster, which is sufficient for experiments §6.2.1/6.2.2/6.2.3,
//! §6.5 sensitivity, and §6.6 overhead.
//!
//! Algorithm (aggregate-only):
//!
//!     Params: r (event refill), C (capacity), t (time refill rate)
//!     State:  B_agg ← C, last_tick ← none
//!
//!     on_request(req):
//!         if req.attempt == 1: ADMIT                     // first attempts always pass
//!         time_refill: B_agg += t·Δt, cap at C
//!         if B_agg < 1: REJECT (local 429)
//!         B_agg -= 1; ADMIT
//!
//!     on_response(req, resp):
//!         if resp.success: B_agg = min(B_agg + r, C)     // event refill
//!
//! Retry identification follows paper §5: an HTTP header (`x-attempt-number`
//! by default) carries a per-request attempt counter. Counter == 1 means first
//! attempt (always admitted); counter > 1 means retry (subject to admission).
//! The originating client or local proxy is responsible for setting and
//! incrementing this header.

use proxy_wasm::traits::*;
use proxy_wasm::types::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::rc::Rc;
use std::time::SystemTime;

proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Info);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> {
        Box::new(ArollaRoot::default())
    });
}}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize, Clone)]
#[serde(default)]
struct Config {
    /// Event refill: tokens deposited on each successful response. Paper: r.
    r: f64,
    /// Bucket capacity (maximum tokens). Paper: C.
    capacity: f64,
    /// Time refill rate: tokens added per second independent of traffic. Paper: t.
    t: f64,
    /// Primary header carrying the per-request attempt counter.
    /// attempt == 1 → first attempt (always admitted); > 1 → retry (gated).
    attempt_header: String,
    /// Secondary attempt header. Checked when the primary is absent or 1.
    /// This supports dual-source retry identification: mesh retries set
    /// `x-envoy-attempt-count` (sidecar outbound), while client retries
    /// set a custom header like `X-Attempt-Number` (survives the gateway,
    /// which strips `x-envoy-*` from external requests).
    attempt_header_secondary: String,
    /// HTTP status returned when a retry is rejected.
    reject_status: u32,
}

impl Default for Config {
    fn default() -> Self {
        // Paper defaults (§5 Configuration): r=0.1, C=10, t=1.0.
        Self {
            r: 0.1,
            capacity: 10.0,
            t: 1.0,
            attempt_header: "x-envoy-attempt-count".to_string(),
            attempt_header_secondary: "X-Attempt-Number".to_string(),
            reject_status: 429,
        }
    }
}

// ---------------------------------------------------------------------------
// Token bucket — shared across all HTTP contexts on this Wasm VM
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
struct Bucket {
    tokens: f64,
    last_refill: Option<SystemTime>,
}

impl Bucket {
    fn new(initial: f64) -> Self {
        Self {
            tokens: initial,
            last_refill: None,
        }
    }

    /// Lazy time refill: add t·Δt tokens since the previous call, cap at `capacity`.
    /// Called on every request (before admission check) and on every response
    /// (before event refill) so the bucket is always current.
    fn refill_time(&mut self, now: SystemTime, t: f64, capacity: f64) {
        match self.last_refill {
            None => {
                self.last_refill = Some(now);
            }
            Some(prev) => {
                // `duration_since` returns Err on clock rewind; treat as no-op.
                if let Ok(dt) = now.duration_since(prev) {
                    self.tokens = (self.tokens + t * dt.as_secs_f64()).min(capacity);
                }
                self.last_refill = Some(now);
            }
        }
    }

    /// Try to claim one token for an admitted retry. Returns `true` iff a
    /// whole token was available and consumed.
    fn try_admit(&mut self) -> bool {
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    /// Event refill: deposit `r` tokens on a successful response, cap at `capacity`.
    fn deposit(&mut self, r: f64, capacity: f64) {
        self.tokens = (self.tokens + r).min(capacity);
    }
}

// ---------------------------------------------------------------------------
// Envoy metrics
// ---------------------------------------------------------------------------

/// Metric IDs defined once at root scope. IDs are small integers that
/// `proxy_wasm::hostcalls::{increment,record}_metric` take on the hot path.
#[derive(Default, Debug, Clone, Copy)]
struct MetricIds {
    admitted: u32,
    rejected: u32,
    first_attempts: u32,
    tokens_gauge: u32,
}

fn define_counter(name: &str) -> u32 {
    proxy_wasm::hostcalls::define_metric(MetricType::Counter, name).unwrap_or(0)
}

fn define_gauge(name: &str) -> u32 {
    proxy_wasm::hostcalls::define_metric(MetricType::Gauge, name).unwrap_or(0)
}

// ---------------------------------------------------------------------------
// Root context — owns the shared bucket and metric IDs
// ---------------------------------------------------------------------------

#[derive(Default)]
struct ArollaRoot {
    config: Config,
    bucket: Rc<RefCell<Bucket>>,
    metrics: MetricIds,
}

impl Context for ArollaRoot {}

impl RootContext for ArollaRoot {
    fn on_configure(&mut self, _config_size: usize) -> bool {
        // Parse plugin config from WasmPlugin.spec.pluginConfig (JSON object).
        if let Some(bytes) = self.get_plugin_configuration() {
            match serde_json::from_slice::<Config>(&bytes) {
                Ok(cfg) => {
                    self.config = cfg;
                    proxy_wasm::hostcalls::log(
                        LogLevel::Info,
                        &format!("arolla: configured {:?}", self.config),
                    )
                    .ok();
                }
                Err(e) => {
                    proxy_wasm::hostcalls::log(
                        LogLevel::Error,
                        &format!("arolla: config parse error ({}); using defaults", e),
                    )
                    .ok();
                }
            }
        }

        // Warm-start the bucket at capacity (Algorithm 1 line 2: B_agg ← C).
        self.bucket.replace(Bucket::new(self.config.capacity));

        // Define Envoy stats once; HTTP contexts reuse the cached IDs.
        self.metrics = MetricIds {
            admitted: define_counter("arolla_retries_admitted_total"),
            rejected: define_counter("arolla_retries_rejected_total"),
            first_attempts: define_counter("arolla_first_attempts_total"),
            tokens_gauge: define_gauge("arolla_bucket_tokens"),
        };

        true
    }

    fn create_http_context(&self, _context_id: u32) -> Option<Box<dyn HttpContext>> {
        Some(Box::new(ArollaHttp {
            config: self.config.clone(),
            bucket: self.bucket.clone(),
            metrics: self.metrics,
        }))
    }

    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }
}

// ---------------------------------------------------------------------------
// HTTP context — one per request
// ---------------------------------------------------------------------------

struct ArollaHttp {
    config: Config,
    bucket: Rc<RefCell<Bucket>>,
    metrics: MetricIds,
}

impl Context for ArollaHttp {}

impl HttpContext for ArollaHttp {
    fn on_http_request_headers(&mut self, _n: usize, _eos: bool) -> Action {
        // Read attempt counter from both headers and take the max. This handles
        // two distinct retry sources:
        //   - Mesh retries (sidecar outbound): Envoy sets x-envoy-attempt-count
        //   - Client retries (through gateway): client sets X-Attempt-Number
        //     (x-envoy-* headers are stripped by the gateway)
        // attempt == 1 if both headers are absent or unparseable.
        let a1: u32 = self
            .get_http_request_header(&self.config.attempt_header)
            .and_then(|v| v.parse().ok())
            .unwrap_or(1);
        let a2: u32 = self
            .get_http_request_header(&self.config.attempt_header_secondary)
            .and_then(|v| v.parse().ok())
            .unwrap_or(1);
        let attempt = a1.max(a2);

        if attempt <= 1 {
            // Paper §5: first attempts are always admitted — we never gate them.
            let _ = proxy_wasm::hostcalls::increment_metric(self.metrics.first_attempts, 1);
            return Action::Continue;
        }

        // Retry path: lazy time-refill, then try to claim a token.
        let now = self.get_current_time();
        let mut bucket = self.bucket.borrow_mut();
        bucket.refill_time(now, self.config.t, self.config.capacity);

        if bucket.try_admit() {
            let tokens_after = bucket.tokens;
            drop(bucket);
            let _ = proxy_wasm::hostcalls::increment_metric(self.metrics.admitted, 1);
            let _ = proxy_wasm::hostcalls::record_metric(
                self.metrics.tokens_gauge,
                tokens_after as u64,
            );
            Action::Continue
        } else {
            let tokens_after = bucket.tokens;
            drop(bucket);
            let _ = proxy_wasm::hostcalls::increment_metric(self.metrics.rejected, 1);
            let _ = proxy_wasm::hostcalls::record_metric(
                self.metrics.tokens_gauge,
                tokens_after as u64,
            );
            self.send_http_response(
                self.config.reject_status,
                vec![("x-arolla-rejected", "1")],
                Some(b"retry rejected by arolla\n"),
            );
            Action::Pause
        }
    }

    fn on_http_response_headers(&mut self, _n: usize, _eos: bool) -> Action {
        // Event refill on successful responses. Paper §4.2 defines "successful"
        // by whatever the downstream considers a success; we use HTTP status
        // < 500 here (2xx/3xx/4xx, excluding our own 429 reject echo).
        //
        // gRPC note: gRPC-over-HTTP/2 always returns HTTP :status 200 even on
        // logical errors; the real outcome is in the `grpc-status` trailer.
        // This MVP approximates by treating status 200 as success, which
        // over-credits gRPC errors. A future revision should also inspect
        // `grpc-status` in on_http_response_trailers().
        let status: u32 = self
            .get_http_response_header(":status")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);

        let is_reject_echo = status == self.config.reject_status;
        let is_success = status > 0 && status < 500 && !is_reject_echo;

        if is_success {
            let now = self.get_current_time();
            let mut bucket = self.bucket.borrow_mut();
            // Refill time first so the event deposit sits on top of current state.
            bucket.refill_time(now, self.config.t, self.config.capacity);
            bucket.deposit(self.config.r, self.config.capacity);
            let tokens_after = bucket.tokens;
            drop(bucket);
            let _ = proxy_wasm::hostcalls::record_metric(
                self.metrics.tokens_gauge,
                tokens_after as u64,
            );
        }
        Action::Continue
    }
}
