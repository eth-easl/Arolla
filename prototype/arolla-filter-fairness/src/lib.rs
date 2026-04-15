//! Arolla — server-side retry admission with per-tenant max-min fairness.
//!
//! Extends the aggregate mixed-refill token bucket (§4.2) with per-tenant
//! retry allocation (§4.3) to prevent aggressive callers from monopolizing
//! retry capacity.
//!
//! Algorithm (paper Algorithm 1):
//!
//!     Params: r (event refill), C (capacity), t (time refill rate)
//!     Config: tenant_header (identifies callers), window_s (sliding window)
//!     State:  B_agg ← C, per-tenant counters (demand, admitted)
//!
//!     on_request(req):
//!         if req.attempt == 1: ADMIT
//!         tenant ← req.header[tenant_header] (or "__default__")
//!         time_refill: B_agg += t·Δt, cap at C
//!
//!         // Aggregate gate (line 8): total admitted never exceeds B_agg
//!         if B_agg < 1: REJECT
//!
//!         // Per-tenant fairness
//!         fair_share ← R_total / N_active
//!         if tenant.admitted < fair_share:
//!             // Guaranteed phase (line 10): below fair share, always admit
//!             B_agg -= 1; tenant.admitted += 1; ADMIT
//!         else:
//!             // Leftover phase (line 13): above fair share, admit only
//!             // if there is leftover capacity from low-demand tenants
//!             leftover ← R_total - sum(min(demand_i, fair_share) for all i)
//!             tenant_leftover_used ← tenant.admitted - fair_share
//!             if tenant_leftover_used < leftover share:
//!                 B_agg -= 1; tenant.admitted += 1; ADMIT
//!             else:
//!                 REJECT
//!
//!     on_response(req, resp):
//!         if resp.success: B_agg = min(B_agg + r, C)
//!
//! When `tenant_header` is empty, per-tenant tracking is disabled and the
//! filter behaves identically to the aggregate-only version.

use proxy_wasm::traits::*;
use proxy_wasm::types::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::collections::HashMap;
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
    /// Event refill: tokens deposited on each successful response.
    r: f64,
    /// Bucket capacity (maximum tokens).
    capacity: f64,
    /// Time refill rate: tokens added per second.
    t: f64,
    /// Primary header carrying the per-request attempt counter.
    attempt_header: String,
    /// Secondary attempt header (for dual-source retry identification).
    attempt_header_secondary: String,
    /// HTTP status returned when a retry is rejected.
    reject_status: u32,

    // ---- Per-tenant fairness (§4.3) ----

    /// Header that identifies the tenant. If empty, per-tenant tracking is
    /// disabled and the filter runs in aggregate-only mode.
    tenant_header: String,
    /// Sliding window duration in seconds for per-tenant accounting.
    window_s: f64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            r: 0.1,
            capacity: 10.0,
            t: 1.0,
            attempt_header: "x-envoy-attempt-count".to_string(),
            attempt_header_secondary: "X-Attempt-Number".to_string(),
            reject_status: 429,
            tenant_header: String::new(), // empty = aggregate-only
            window_s: 10.0,
        }
    }
}

// ---------------------------------------------------------------------------
// Per-tenant state
// ---------------------------------------------------------------------------

/// Tracks each tenant's retry demand and admitted retries within the current
/// sliding window for max-min fair allocation.
#[derive(Debug, Clone)]
struct TenantState {
    /// Number of retries requested in the current window (demand).
    demand: u64,
    /// Number of retries admitted in the current window.
    admitted: u64,
}

impl TenantState {
    fn new() -> Self {
        Self {
            demand: 0,
            admitted: 0,
        }
    }
}

// ---------------------------------------------------------------------------
// Aggregate bucket + fairness state
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
struct SharedState {
    // ---- Aggregate token bucket (Algorithm 1, line 2) ----
    tokens: f64,
    last_refill: Option<SystemTime>,

    // ---- Per-tenant fairness (Algorithm 1, lines 5-6, 9-13) ----
    tenants: HashMap<String, TenantState>,
    /// Start of the current sliding window (Algorithm 1, line 17-19).
    window_start: Option<SystemTime>,
}

impl SharedState {
    fn new(capacity: f64) -> Self {
        Self {
            tokens: capacity,
            last_refill: None,
            tenants: HashMap::new(),
            window_start: None,
        }
    }

    /// Lazy time refill (Algorithm 1, line 7): B_agg += t · Δt, cap at C.
    fn refill_time(&mut self, now: SystemTime, t: f64, capacity: f64) {
        match self.last_refill {
            None => {
                self.last_refill = Some(now);
            }
            Some(prev) => {
                if let Ok(dt) = now.duration_since(prev) {
                    self.tokens = (self.tokens + t * dt.as_secs_f64()).min(capacity);
                }
                self.last_refill = Some(now);
            }
        }
    }

    /// Event refill (Algorithm 1, line 16): B_agg ← min(B_agg + r, C).
    fn deposit(&mut self, r: f64, capacity: f64) {
        self.tokens = (self.tokens + r).min(capacity);
    }

    /// Window boundary (Algorithm 1, lines 17-19): reset counters, evict
    /// inactive tenants.
    fn maybe_reset_window(&mut self, now: SystemTime, window_s: f64) {
        let should_reset = match self.window_start {
            None => true,
            Some(start) => {
                if let Ok(elapsed) = now.duration_since(start) {
                    elapsed.as_secs_f64() >= window_s
                } else {
                    false
                }
            }
        };
        if should_reset {
            // Line 18: Reset demand[·], admitted[·]
            for ts in self.tenants.values_mut() {
                ts.demand = 0;
                ts.admitted = 0;
            }
            // Line 19: Evict inactive tenants.
            self.tenants.retain(|_, ts| ts.demand > 0 || ts.admitted > 0);
            self.window_start = Some(now);
        }
    }

    /// Algorithm 1, lines 4-13: per-tenant admission.
    fn try_admit_tenant(&mut self, tenant: &str) -> bool {
        // Line 6: demand[id] += 1
        self.tenants
            .entry(tenant.to_string())
            .or_insert_with(TenantState::new)
            .demand += 1;

        // Line 8: if B_agg < 1 then return REJECT
        if self.tokens < 1.0 {
            return false;
        }

        // Line 9: share ← B_agg / N_active
        let n_active = self.tenants.values().filter(|ts| ts.demand > 0).count();
        let share = if n_active > 0 {
            self.tokens / n_active as f64
        } else {
            0.0
        };

        // Read admitted count (borrow-checker friendly).
        let admitted = self
            .tenants
            .get(tenant)
            .map(|ts| ts.admitted)
            .unwrap_or(0);

        // Line 10-11: if admitted[id] < share → guaranteed ADMIT
        if (admitted as f64) < share {
            self.tokens -= 1.0;
            self.tenants.get_mut(tenant).unwrap().admitted += 1;
            return true;
        }

        // Line 12-13: else if B_agg ≥ 1 → leftover ADMIT
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            self.tenants.get_mut(tenant).unwrap().admitted += 1;
            return true;
        }

        // return REJECT
        false
    }

    /// Aggregate-only admission (no tenant tracking).
    fn try_admit_aggregate(&mut self) -> bool {
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

// ---------------------------------------------------------------------------
// Envoy metrics
// ---------------------------------------------------------------------------

#[derive(Default, Debug, Clone, Copy)]
struct MetricIds {
    admitted: u32,
    rejected: u32,
    first_attempts: u32,
    tokens_gauge: u32,
    // Per-tenant metrics are tracked via response headers (not Envoy stats)
    // because Envoy stats don't support dynamic label dimensions. The
    // experiment analyzer reads tenant identity from client CSV columns.
}

fn define_counter(name: &str) -> u32 {
    proxy_wasm::hostcalls::define_metric(MetricType::Counter, name).unwrap_or(0)
}

fn define_gauge(name: &str) -> u32 {
    proxy_wasm::hostcalls::define_metric(MetricType::Gauge, name).unwrap_or(0)
}

// ---------------------------------------------------------------------------
// Root context
// ---------------------------------------------------------------------------

#[derive(Default)]
struct ArollaRoot {
    config: Config,
    state: Rc<RefCell<SharedState>>,
    metrics: MetricIds,
}

impl Context for ArollaRoot {}

impl RootContext for ArollaRoot {
    fn on_configure(&mut self, _config_size: usize) -> bool {
        if let Some(bytes) = self.get_plugin_configuration() {
            match serde_json::from_slice::<Config>(&bytes) {
                Ok(cfg) => {
                    self.config = cfg;
                    proxy_wasm::hostcalls::log(
                        LogLevel::Info,
                        &format!("arolla-fairness: configured {:?}", self.config),
                    )
                    .ok();
                }
                Err(e) => {
                    proxy_wasm::hostcalls::log(
                        LogLevel::Error,
                        &format!(
                            "arolla-fairness: config parse error ({}); using defaults",
                            e
                        ),
                    )
                    .ok();
                }
            }
        }

        let mode = if self.config.tenant_header.is_empty() {
            "aggregate-only"
        } else {
            "per-tenant fairness"
        };
        proxy_wasm::hostcalls::log(
            LogLevel::Info,
            &format!(
                "arolla-fairness: mode={}, r={}, C={}, t={}, window={}s, tenant_header='{}'",
                mode,
                self.config.r,
                self.config.capacity,
                self.config.t,
                self.config.window_s,
                self.config.tenant_header,
            ),
        )
        .ok();

        self.state.replace(SharedState::new(self.config.capacity));

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
            state: self.state.clone(),
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
    state: Rc<RefCell<SharedState>>,
    metrics: MetricIds,
}

impl Context for ArollaHttp {}

impl HttpContext for ArollaHttp {
    fn on_http_request_headers(&mut self, _n: usize, _eos: bool) -> Action {
        // Read attempt counter from both headers, take the max.
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
            let _ = proxy_wasm::hostcalls::increment_metric(self.metrics.first_attempts, 1);
            return Action::Continue;
        }

        // ---- Retry admission ----
        let now = self.get_current_time();
        let mut st = self.state.borrow_mut();
        st.refill_time(now, self.config.t, self.config.capacity);

        let admitted = if self.config.tenant_header.is_empty() {
            // Aggregate-only mode: no per-tenant tracking.
            st.try_admit_aggregate()
        } else {
            // Per-tenant fairness mode.
            st.maybe_reset_window(now, self.config.window_s);

            let tenant = self
                .get_http_request_header(&self.config.tenant_header)
                .unwrap_or_else(|| "__default__".to_string());

            st.try_admit_tenant(&tenant)
        };

        let tokens_after = st.tokens;
        drop(st);

        if admitted {
            let _ = proxy_wasm::hostcalls::increment_metric(self.metrics.admitted, 1);
            let _ =
                proxy_wasm::hostcalls::record_metric(self.metrics.tokens_gauge, tokens_after as u64);
            Action::Continue
        } else {
            let _ = proxy_wasm::hostcalls::increment_metric(self.metrics.rejected, 1);
            let _ =
                proxy_wasm::hostcalls::record_metric(self.metrics.tokens_gauge, tokens_after as u64);
            self.send_http_response(
                self.config.reject_status,
                vec![("x-arolla-rejected", "1")],
                Some(b"retry rejected by arolla\n"),
            );
            Action::Pause
        }
    }

    fn on_http_response_headers(&mut self, _n: usize, _eos: bool) -> Action {
        let status: u32 = self
            .get_http_response_header(":status")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);

        let is_reject_echo = status == self.config.reject_status;
        let is_success = status > 0 && status < 500 && !is_reject_echo;

        if is_success {
            let now = self.get_current_time();
            let mut st = self.state.borrow_mut();
            st.refill_time(now, self.config.t, self.config.capacity);
            st.deposit(self.config.r, self.config.capacity);
            let tokens_after = st.tokens;
            drop(st);
            let _ =
                proxy_wasm::hostcalls::record_metric(self.metrics.tokens_gauge, tokens_after as u64);
        }
        Action::Continue
    }
}
