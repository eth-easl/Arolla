//! Arolla fairness — server-side retry admission with attempt-aware cost.
//!
//! Builds on the aggregate mixed-refill token bucket (arolla-filter). Adds a
//! simple attempt-aware cost function so that deep retries pay exponentially
//! more than shallow retries, while still respecting the bucket capacity.
//!
//! Rationale:
//!   - Shallow retries (1st-2nd retry) usually serve transient errors — cheap.
//!   - Deep retries (4th+ retry) usually indicate persistent failure — expensive.
//!   - A single aggressive client at attempt=10 costs 10 tokens, draining half
//!     a C=20 bucket in one admission. Polite clients (≤3 retries) cost 1 each.
//!   - This respects demand (everyone can retry) but prevents aggressive clients
//!     from stealing retry capacity with deep retry chains.
//!
//! Admission rule:
//!
//!     on_request(req):
//!         if req.attempt == 1: ADMIT                    (first attempts bypass)
//!         time_refill: B_agg += t·Δt, cap at C
//!         cost ← 1 if attempt <= cheap_threshold else attempt
//!         if B_agg < cost: REJECT (local 429)
//!         B_agg -= cost; ADMIT
//!
//!     on_response(req, resp):
//!         if resp.success: B_agg = min(B_agg + r, C)    (event refill)
//!
//! Strictly simpler than Algorithm 1 fairness (§4.3):
//!   - No per-tenant state (no HashMap, no window reset).
//!   - No tenant_header needed.
//!   - Fairness emerges from the attempt-vs-cost relationship on a shared bucket.

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
    /// Time refill rate: tokens added per second. Paper: t.
    t: f64,
    /// Primary header carrying the per-request attempt counter.
    attempt_header: String,
    /// Secondary attempt header.
    attempt_header_secondary: String,
    /// HTTP status returned when a retry is rejected.
    reject_status: u32,
    /// Attempts at or below this threshold cost 1 token (cheap path).
    /// Attempts above this threshold are priced by `cost_mode`.
    /// Default: 3 → 1st and 2nd retry are cheap; 3rd+ escalates.
    cheap_threshold: u32,
    /// Cost schedule for attempts above `cheap_threshold`:
    ///   "linear"     → cost = attempt             (attempt 5 costs 5)
    ///   "triangular" → cost = attempt*(attempt-1)/2  (attempt 5 costs 10)
    ///   "quadratic"  → cost = attempt * attempt      (attempt 5 costs 25)
    cost_mode: String,
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
            cheap_threshold: 3,
            cost_mode: "linear".to_string(),
        }
    }
}

/// Cost schedules for deep retries (attempt > cheap_threshold).
#[derive(Debug, Clone, Copy)]
enum CostMode {
    Linear,
    Triangular,
    Quadratic,
}

impl CostMode {
    fn from_str(s: &str) -> Self {
        match s.to_ascii_lowercase().as_str() {
            "triangular" => CostMode::Triangular,
            "quadratic" => CostMode::Quadratic,
            _ => CostMode::Linear,
        }
    }

    fn cost(self, attempt: u32) -> f64 {
        let a = attempt as f64;
        match self {
            CostMode::Linear => a,
            CostMode::Triangular => a * (a - 1.0) / 2.0,
            CostMode::Quadratic => a * a,
        }
    }
}

// ---------------------------------------------------------------------------
// Token bucket
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

    /// Fairness admission with attempt-aware cost.
    ///
    ///   attempt <= cheap_threshold  → cost = 1 token
    ///   attempt >  cheap_threshold  → cost = cost_mode.cost(attempt)
    ///
    /// Reject if tokens < cost(attempt), else consume cost tokens.
    /// Returns (admitted, cost_applied) so the caller can log the cost.
    fn try_admit_fairness(
        &mut self,
        attempt: u32,
        cheap_threshold: u32,
        cost_mode: CostMode,
    ) -> (bool, f64) {
        let cost: f64 = if attempt <= cheap_threshold {
            1.0
        } else {
            cost_mode.cost(attempt)
        };
        if self.tokens < cost {
            return (false, cost);
        }
        self.tokens -= cost;
        (true, cost)
    }

    fn deposit(&mut self, r: f64, capacity: f64) {
        self.tokens = (self.tokens + r).min(capacity);
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
    cost_mode: Option<CostMode>,
    bucket: Rc<RefCell<Bucket>>,
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

        self.bucket.replace(Bucket::new(self.config.capacity));
        self.cost_mode = Some(CostMode::from_str(&self.config.cost_mode));

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
            cost_mode: self.cost_mode.unwrap_or(CostMode::Linear),
            bucket: self.bucket.clone(),
            metrics: self.metrics,
        }))
    }

    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }
}

// ---------------------------------------------------------------------------
// HTTP context
// ---------------------------------------------------------------------------

struct ArollaHttp {
    config: Config,
    cost_mode: CostMode,
    bucket: Rc<RefCell<Bucket>>,
    metrics: MetricIds,
}

impl Context for ArollaHttp {}

impl HttpContext for ArollaHttp {
    fn on_http_request_headers(&mut self, _n: usize, _eos: bool) -> Action {
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

        // Retry path: lazy time-refill, then attempt-aware admission.
        let now = self.get_current_time();
        let mut bucket = self.bucket.borrow_mut();
        bucket.refill_time(now, self.config.t, self.config.capacity);

        let (admitted, _cost) = bucket.try_admit_fairness(
            attempt, self.config.cheap_threshold, self.cost_mode,
        );
        if admitted {
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
                Some(b"retry rejected by arolla (fairness)\n"),
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
            let mut bucket = self.bucket.borrow_mut();
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
