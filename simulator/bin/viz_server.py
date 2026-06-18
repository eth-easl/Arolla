#!/usr/bin/env python3
"""
FastAPI server wrapping the Python simulator for interactive JSX visualizations.

Usage:
    cd simulator && python bin/viz_server.py
    # or: cd simulator && uvicorn bin.viz_server:app --reload --port 8642

Endpoints:
    GET  /                  — Landing page with links to all visualizations
    GET  /viz/{name}        — Render a JSX visualization in the browser
    POST /api/run           — Run a single experiment from a YAML config path
    POST /api/run_inline    — Run a single experiment from inline YAML string
    POST /api/sweep         — Run a parameter sweep (vary one param)
    POST /api/scaling       — Run scaling study (vary client count)
    POST /api/multi_client_sweep — Run multi-client 2D sweep (p_fail × replicas)
    POST /api/chain         — Run chain depth study
    POST /api/heatmap       — Run 2D parameter heatmap sweep
    GET  /api/configs       — List available YAML configs
"""

import sys
import json
import math
import time
import hashlib
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from simulator.config.loader import ConfigLoader
from simulator.config.schema import ExperimentConfig
from simulator.utils.time import s_to_ns

app = FastAPI(title="Retry Budget Simulator API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache for recent results
_cache = {}

YAML_DIR = Path(__file__).parent.parent / "experiments" / "yaml"
JSX_DIR = Path(__file__).parent.parent / "viz"
RESULTS_DIR = Path(__file__).parent.parent / "results"


# ============================================================
# Core simulation runner
# ============================================================

def run_simulation(config: ExperimentConfig) -> dict:
    """Run a simulation and return structured results."""
    sim, clients, workloads, fault_tracker, services = ConfigLoader.build_simulation(config)

    max_duration = max(wl.duration_s for wl in workloads)
    for client, workload in zip(clients, workloads):
        workload.drive(sim, lambda s, c=client: c.start_request(s))

    sim.run(until=s_to_ns(max_duration))
    sim.run()  # drain

    def _safe(v):
        """Ensure float is JSON-serializable (replace NaN/Inf with 0)."""
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return 0
        return v

    # Collect per-client summaries
    client_results = []
    for client in clients:
        metrics = client.metrics()
        summary = metrics.summary()
        df = metrics.to_dataframe(granularity_s=config.granularity_s)

        # Add derived metric columns before NaN fill
        if not df.empty:
            df['goodput_rps'] = df['success_root'] / config.granularity_s
            df['retry_efficiency'] = np.where(
                df['retries'] > 0,
                (df['retries'] - df['failure_retry']) / df['retries'],
                0.0,
            )
            df['amplification_factor'] = np.where(
                df['root_requests'] > 0,
                (df['root_requests'] + df['retries']) / df['root_requests'],
                1.0,
            )
            # Replace NaN with 0 for JSON serialization
            df = df.fillna(0)

        # Summary-level derived metrics
        total_retries_sum = float(df['retries'].sum()) if not df.empty else 0
        retry_fails_sum = float(df['failure_retry'].sum()) if not df.empty else 0
        retry_eff = (
            (total_retries_sum - retry_fails_sum) / total_retries_sum
            if total_retries_sum > 0 else 0.0
        )

        client_results.append({
            "name": client.cfg.name,
            "summary": {
                "total": summary.total,
                "succeeded": summary.succeeded,
                "dropped_queue": summary.dropped_queue,
                "dropped_deadline": summary.dropped_deadline,
                "dropped_server_failure": summary.dropped_server_failure,
                "success_rate": summary.succeeded / summary.total if summary.total > 0 else 0,
                "p50": _safe(round(summary.p50, 2)),
                "p90": _safe(round(summary.p90, 2)),
                "p95": _safe(round(summary.p95, 2)),
                "p99": _safe(round(summary.p99, 2)),
                "mean": _safe(round(summary.mean, 2)),
                "retries_per_root": _safe(round(summary.retries_per_root, 3)),
                "goodput_rps": _safe(round(summary.succeeded / max_duration, 2)) if max_duration > 0 else 0,
                "amplification_factor": _safe(round(
                    summary.attempts_total / summary.total if summary.total > 0 else 1.0, 3)),
                "retry_efficiency": _safe(round(retry_eff, 3)),
            },
            "timeseries": df.to_dict(orient="records"),
        })

    # Per-service metrics if multi-service
    service_results = []
    if len(services) > 1:
        from simulator.metrics.service_collector import collect_service_metrics
        svc_df = collect_service_metrics(services, granularity_s=config.granularity_s)
        if not svc_df.empty:
            service_results = svc_df.to_dict(orient="records")

    # Fault events
    fault_events = []
    if fault_tracker:
        fault_events = [
            {
                "event_type": e.event_type,
                "start_time_s": e.start_time_s,
                "end_time_s": e.end_time_s,
                "parameters": e.parameters,
            }
            for e in fault_tracker.events
        ]

    # Compute recovery time per client
    for cr in client_results:
        cr["summary"]["recovery_time_s"] = _compute_recovery_time(
            cr["timeseries"], fault_events
        )

    # Compute fairness share shift (multi-client only)
    fairness = _compute_fairness_share_shift(client_results, config)

    return {
        "name": config.name,
        "clients": client_results,
        "services": service_results,
        "fault_events": fault_events,
        "fairness_share_shift": fairness,
    }


# ============================================================
# Derived metric helpers
# ============================================================

def _compute_recovery_time(
    timeseries: list, fault_events: list, threshold: float = 0.95
) -> Optional[float]:
    """Seconds from last fault removal to goodput returning to `threshold` of pre-fault baseline.

    Returns None if no fault events, no recovery detected, or insufficient data.
    """
    if not fault_events or not timeseries:
        return None

    last_fault_end = max(
        fe["end_time_s"] for fe in fault_events if "end_time_s" in fe
    )
    first_fault_start = min(
        fe["start_time_s"] for fe in fault_events if "start_time_s" in fe
    )

    # Baseline goodput = mean goodput_rps before any fault
    baseline_buckets = [
        b for b in timeseries if b["timepoint"] < first_fault_start
    ]
    if not baseline_buckets:
        return None
    baseline_goodput = sum(b.get("goodput_rps", 0) for b in baseline_buckets) / len(
        baseline_buckets
    )
    if baseline_goodput <= 0:
        return None

    target = threshold * baseline_goodput

    # Scan forward from fault end
    post_fault = sorted(
        (b for b in timeseries if b["timepoint"] >= last_fault_end),
        key=lambda b: b["timepoint"],
    )
    for b in post_fault:
        if b.get("goodput_rps", 0) >= target:
            return round(b["timepoint"] - last_fault_end, 2)

    return None  # never recovered


def _compute_fairness_share_shift(
    client_results: list, config: ExperimentConfig
) -> Optional[float]:
    """Worst-case goodput share shift: max_t max_i (actual_share_i(t) - fair_share_i).

    fair_share_i is proportional to the client's configured base_rps.
    A positive value means some client grabs more than its fair share.
    Returns None for single-client experiments.
    """
    if len(client_results) < 2:
        return None

    # Determine each client's fair share from configured RPS
    rps_list = []
    for i, cr in enumerate(client_results):
        # Try to read the configured base_rps from the experiment config
        rps = 100.0  # default fallback
        if hasattr(config, "clients") and i < len(config.clients):
            wl = getattr(config.clients[i], "workload", None)
            if wl and hasattr(wl, "base_rps"):
                rps = float(wl.base_rps)
        rps_list.append(rps)

    total_rps = sum(rps_list)
    fair_shares = [r / total_rps for r in rps_list]

    # Index timeseries by timepoint for each client
    ts_by_client = []
    all_timepoints = set()
    for cr in client_results:
        ts_map = {}
        for b in cr.get("timeseries", []):
            tp = b["timepoint"]
            ts_map[tp] = b.get("goodput_rps", 0)
            all_timepoints.add(tp)
        ts_by_client.append(ts_map)

    max_shift = 0.0
    for t in all_timepoints:
        goodputs = [ts.get(t, 0) for ts in ts_by_client]
        total_g = sum(goodputs)
        if total_g <= 0:
            continue
        for i, g in enumerate(goodputs):
            shift = (g / total_g) - fair_shares[i]
            if shift > max_shift:
                max_shift = shift

    return round(max_shift, 4)


# ============================================================
# API Models
# ============================================================

class RunConfigRequest(BaseModel):
    config_path: str = Field(description="Path to YAML config relative to experiments/yaml/")

class RunInlineRequest(BaseModel):
    yaml_content: str = Field(description="YAML config content as a string")

class SweepRequest(BaseModel):
    config_path: str = Field(description="Base YAML config path")
    param_path: str = Field(description="Dot-separated path to parameter to sweep, e.g. 'clients.0.retry.max_attempts'")
    values: list = Field(description="List of values to sweep over")

class ScalingRequest(BaseModel):
    config_path: str = Field(description="Base YAML config path")
    client_counts: list[int] = Field(default=[1, 5, 10, 50, 100])
    total_rps: float = Field(default=200.0)

class ChainRequest(BaseModel):
    config_path: str = Field(description="Base chain YAML config path")
    depths: list[int] = Field(default=[1, 2, 3, 4, 5])

class HeatmapRequest(BaseModel):
    config_path: str = Field(description="Base YAML config path")
    param_path_x: str = Field(description="Dot-separated path to X-axis parameter")
    values_x: list = Field(description="X-axis parameter values to sweep")
    param_path_y: str = Field(description="Dot-separated path to Y-axis parameter")
    values_y: list = Field(description="Y-axis parameter values to sweep")

class MultiClientSweepRequest(BaseModel):
    config_path: str = Field(description="Multi-client YAML config path")
    p_fail_values: Optional[list[float]] = Field(default=None, description="Override p_fail sweep values")
    replicas_values: Optional[list[int]] = Field(default=None, description="Override replicas sweep values")
    fixed_total_rps: Optional[float] = Field(default=None, description="Keep total RPS constant (divide by N). None = use per-client base_rps as-is.")

class ParamCompareRequest(BaseModel):
    config_path: str = Field(description="Base YAML config path")
    param_name: str = Field(description="Short parameter name, e.g. 'p_fail', 'base_rps', 'workers'")
    values: list[float] = Field(description="Parameter values to compare")


# ============================================================
# Frontend — serves JSX visualizations in the browser
# ============================================================

VIZ_PAGES = [
    {"slug": "retry-control-sensitivity", "title": "Single-Client & Single-Service", "desc": "Compare retry strategies: parameter sensitivity, goodput, amplification, retry efficiency, recovery time"},
    {"slug": "retry-at-scale", "title": "Multi-Client & Single-Service", "desc": "How retry strategies scale with client count: goodput, amplification, retry efficiency vs N"},
    {"slug": "chain-amplification", "title": "Multi-Service", "desc": "Retry amplification across dependency chains and client diversity"},
]


def _render_page(jsx_source: str, title: str) -> str:
    """Wrap JSX source in an HTML page with React + Babel standalone."""
    # Strip ES module imports — React hooks come from the global React object
    import re
    jsx_source = re.sub(
        r'^import\s+\{[^}]*\}\s+from\s+["\']react["\'];?\s*$',
        "",
        jsx_source,
        flags=re.MULTILINE,
    )
    # Replace bare hook references with React.* since there's no import
    # We inject a preamble that destructures the hooks we need
    preamble = """const { useState, useCallback, useMemo, useEffect, useRef } = React;

// Theme helper — reads CSS variables so JSX components adapt to light/dark mode
function _cv(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function useTheme() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const obs = new MutationObserver(() => setTick(t => t + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);
  return {
    bg: _cv('--viz-bg-gradient'), text: _cv('--viz-text'), muted: _cv('--viz-text-muted'),
    dim: _cv('--viz-text-dim'), faint: _cv('--viz-text-faint'), accent: _cv('--viz-accent'),
    panel: _cv('--viz-panel'), border: _cv('--viz-border'), inputBg: _cv('--viz-input-bg'),
    grid: _cv('--viz-grid'), rowBorder: _cv('--viz-row-border'), hintBg: _cv('--viz-hint-bg'),
    errorBg: _cv('--viz-error-bg'), errorBorder: _cv('--viz-error-border'), errorText: _cv('--viz-error-text'),
    btnBg: _cv('--viz-btn-bg'), chipBg: _cv('--viz-chip-bg'), svgLabel: _cv('--viz-svg-label'), svgAxis: _cv('--viz-svg-axis'),
  };
}
"""
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — Retry Budget Simulator</title>
  <script src="https://unpkg.com/react@18/umd/react.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    :root[data-theme="dark"] {{
      --viz-bg: #070b14;
      --viz-bg-gradient: linear-gradient(145deg, #070b14 0%, #0c1220 40%, #0a1628 100%);
      --viz-text: #e5e7eb;
      --viz-text-muted: #9ca3af;
      --viz-text-dim: #6b7280;
      --viz-text-faint: #4b5563;
      --viz-accent: #67e8f9;
      --viz-panel: rgba(15, 23, 42, 0.7);
      --viz-border: #1e3a5f;
      --viz-input-bg: #0f1729;
      --viz-grid: #1f2937;
      --viz-row-border: #111827;
      --viz-hint-bg: rgba(103, 232, 249, 0.04);
      --viz-error-bg: rgba(239, 68, 68, 0.1);
      --viz-error-border: #7f1d1d;
      --viz-error-text: #fca5a5;
      --viz-btn-bg: rgba(103, 232, 249, 0.1);
      --viz-chip-bg: rgba(103, 232, 249, 0.05);
      --viz-svg-label: #6b7280;
      --viz-svg-axis: #9ca3af;
    }}
    :root[data-theme="light"] {{
      --viz-bg: #f8fafc;
      --viz-bg-gradient: linear-gradient(145deg, #f8fafc 0%, #f1f5f9 40%, #e2e8f0 100%);
      --viz-text: #1e293b;
      --viz-text-muted: #475569;
      --viz-text-dim: #64748b;
      --viz-text-faint: #94a3b8;
      --viz-accent: #0891b2;
      --viz-panel: rgba(255, 255, 255, 0.85);
      --viz-border: #cbd5e1;
      --viz-input-bg: #ffffff;
      --viz-grid: #e2e8f0;
      --viz-row-border: #f1f5f9;
      --viz-hint-bg: rgba(8, 145, 178, 0.04);
      --viz-error-bg: rgba(239, 68, 68, 0.06);
      --viz-error-border: #fecaca;
      --viz-error-text: #dc2626;
      --viz-btn-bg: rgba(8, 145, 178, 0.1);
      --viz-chip-bg: rgba(8, 145, 178, 0.06);
      --viz-svg-label: #64748b;
      --viz-svg-axis: #475569;
    }}
    body {{ background: var(--viz-bg-gradient); overflow-x: hidden; transition: background 0.3s; color: var(--viz-text); }}
    .back-nav {{
      position: fixed; top: 12px; right: 16px; z-index: 1000;
      display: flex; gap: 8px; align-items: center;
    }}
    .back-nav a, .back-nav button {{
      font-family: 'JetBrains Mono', monospace; font-size: 13px;
      padding: 5px 12px; border-radius: 5px; text-decoration: none;
      border: 1px solid var(--viz-border); cursor: pointer; transition: all 0.2s;
      background: var(--viz-panel); color: var(--viz-accent);
    }}
    .back-nav button {{ color: var(--viz-text-muted); }}
    .back-nav a:hover, .back-nav button:hover {{ opacity: 0.8; }}
  </style>
</head>
<body>
  <div class="back-nav">
    <a href="/">Home</a>
    <button onclick="toggleTheme()">Light/Dark</button>
  </div>
  <div id="root"></div>
  <script>
    function toggleTheme() {{
      const html = document.documentElement;
      const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', next);
      localStorage.setItem('theme', next);
    }}
    const saved = localStorage.getItem('theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  </script>
  <script type="text/babel" data-type="module">
{preamble}
{jsx_source}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(React.createElement(App));
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def landing_page():
    """Landing page listing all available visualizations."""
    cards = ""
    for viz in VIZ_PAGES:
        cards += f"""
      <a href="/viz/{viz['slug']}" class="card">
        <div class="card-title">{viz['title']}</div>
        <div class="card-desc">{viz['desc']}</div>
      </a>"""

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Retry Budget Simulator</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}

    :root[data-theme="dark"] {{
      --bg: linear-gradient(145deg, #070b14 0%, #0c1220 40%, #0a1628 100%);
      --text: #e5e7eb;
      --text-muted: #6b7280;
      --text-dim: #4b5563;
      --accent: #67e8f9;
      --accent-secondary: #9ca3af;
      --card-bg: rgba(15, 23, 42, 0.7);
      --border: #1e3a5f;
      --footer-bg: rgba(15, 23, 42, 0.5);
      --toggle-bg: rgba(103, 232, 249, 0.1);
    }}

    :root[data-theme="light"] {{
      --bg: linear-gradient(145deg, #f8fafc 0%, #f1f5f9 40%, #e2e8f0 100%);
      --text: #1e293b;
      --text-muted: #64748b;
      --text-dim: #94a3b8;
      --accent: #0891b2;
      --accent-secondary: #475569;
      --card-bg: rgba(255, 255, 255, 0.8);
      --border: #cbd5e1;
      --footer-bg: rgba(241, 245, 249, 0.8);
      --toggle-bg: rgba(8, 145, 178, 0.1);
    }}

    body {{
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      font-family: 'JetBrains Mono', monospace;
      padding: 40px 20px;
      font-size: 14px;
      transition: background 0.3s, color 0.3s;
    }}

    .card {{
      display: block; padding: 20px 24px; margin-bottom: 12px;
      background: var(--card-bg); border: 1px solid var(--border);
      border-radius: 10px; text-decoration: none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }}
    .card:hover {{
      border-color: var(--accent);
      box-shadow: 0 2px 12px rgba(103, 232, 249, 0.08);
    }}
    .card-title {{
      font-size: 17px; font-weight: 600; color: var(--accent); margin-bottom: 6px;
    }}
    .card-desc {{
      font-size: 13px; color: var(--text-muted); line-height: 1.5;
    }}

    .theme-toggle {{
      background: var(--toggle-bg); border: 1px solid var(--border);
      color: var(--accent); font-family: inherit; font-size: 13px;
      padding: 6px 14px; border-radius: 6px; cursor: pointer;
      transition: all 0.2s;
    }}
    .theme-toggle:hover {{ border-color: var(--accent); }}
  </style>
</head>
<body>
  <div style="max-width: 740px; margin: 0 auto;">
    <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
      <h1 style="font-size: 26px; font-weight: 700; color: var(--accent);">
        Retry Budget Simulator
      </h1>
      <button class="theme-toggle" onclick="toggleTheme()">Toggle Light/Dark</button>
    </div>
    <p style="font-size: 14px; color: var(--text-muted); margin-bottom: 28px; border-bottom: 1px solid var(--border); padding-bottom: 16px;">
      Interactive visualizations powered by the real Python discrete-event simulator
    </p>
    <h2 style="font-size: 13px; color: var(--accent-secondary); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 14px;">
      Visualizations
    </h2>
    {cards}
    <div style="margin-top: 28px; padding: 14px 18px; background: var(--footer-bg); border: 1px solid var(--border); border-radius: 8px; font-size: 12px; color: var(--text-dim); line-height: 1.8;">
      <strong style="color: var(--text-muted);">API endpoints:</strong>
      <a href="/docs" style="color: var(--accent); text-decoration: none;">Swagger docs</a> |
      <a href="/api/configs" style="color: var(--accent); text-decoration: none;">GET /api/configs</a> |
      POST /api/run | POST /api/sweep | POST /api/scaling | POST /api/chain
    </div>
  </div>
  <script>
    function toggleTheme() {{
      const html = document.documentElement;
      const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', next);
      localStorage.setItem('theme', next);
    }}
    // Restore saved preference
    const saved = localStorage.getItem('theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  </script>
</body>
</html>"""


@app.get("/viz/{name}", response_class=HTMLResponse)
async def serve_viz(name: str):
    """Serve a JSX visualization rendered in the browser."""
    jsx_path = JSX_DIR / f"{name}.jsx"
    if not jsx_path.exists():
        raise HTTPException(404, f"Visualization not found: {name}")

    viz_info = next((v for v in VIZ_PAGES if v["slug"] == name), None)
    title = viz_info["title"] if viz_info else name

    jsx_source = jsx_path.read_text()
    return _render_page(jsx_source, title)


# ============================================================
# API Endpoints
# ============================================================

@app.get("/api/configs")
async def list_configs():
    """List all available YAML experiment configs."""
    configs = []
    for p in sorted(YAML_DIR.rglob("*.yaml")):
        rel = p.relative_to(YAML_DIR)
        configs.append(str(rel))
    return {"configs": configs}


@app.get("/api/strategies")
async def get_strategies(config_path: str):
    """Auto-discover strategies and sweepable parameters from a YAML config."""
    import yaml
    yaml_path = YAML_DIR / config_path
    if not yaml_path.exists():
        raise HTTPException(404, f"Config not found: {config_path}")

    with open(yaml_path) as f:
        yaml_dict = yaml.safe_load(f)

    services = yaml_dict.get("services", [])
    clients = yaml_dict.get("clients", [])
    svc_by_name = {s["name"]: (i, s) for i, s in enumerate(services)}

    # Full parameter specs per control type: (param_name, schema_default)
    # schema_default=None means required or disabled-by-default (skip if absent)
    CB_PARAMS = [
        ("failure_threshold", None),
        ("success_threshold", None),
        ("half_open_delay_ms", None),
        ("window_duration_ms", None),
        ("min_requests", None),
        ("failure_window_size", None),
        ("success_window_size", None),
    ]
    RB_PARAMS = [
        ("budget_ratio", None),
        ("max_retries", None),
        ("min_retries_per_sec", 10),
    ]
    AROLLA_PARAMS = [
        ("alpha", 0.1),
        ("beta_down", 0.3),
        ("beta_up", 0.05),
        ("window_ms", 1000.0),
        ("success_rate_threshold", None),
        ("success_rate_beta", 0.1),
        ("max_retry_ratio", None),
    ]

    def _collect_params(config_dict, param_specs, path_prefix):
        """Collect sweepable params from a config block, including schema defaults."""
        sweeps = []
        for param_name, schema_default in param_specs:
            val = config_dict.get(param_name)
            if val is None:
                val = schema_default
            if val is None:
                continue  # truly optional and not set
            sweeps.append({
                "param": param_name,
                "path": f"{path_prefix}.{param_name}",
                "current": val,
                "values": _default_sweep_range(param_name, val),
            })
        return sweeps

    strategies = []
    for ci, client in enumerate(clients):
        name = client.get("name", f"client_{ci}")
        sweeps = []

        # Service-side control parameters only (skip shared retry/timeout config)
        target = client.get("target_service", "")
        if target in svc_by_name:
            si, svc = svc_by_name[target]

            cb = svc.get("circuit_breaker")
            if cb:
                sweeps.extend(_collect_params(cb, CB_PARAMS, f"services.{si}.circuit_breaker"))

            rb = svc.get("retry_budget")
            if rb:
                sweeps.extend(_collect_params(rb, RB_PARAMS, f"services.{si}.retry_budget"))

            ar = svc.get("arolla_retry_budget")
            if ar:
                sweeps.extend(_collect_params(ar, AROLLA_PARAMS, f"services.{si}.arolla_retry_budget"))

        strategies.append({
            "key": name,
            "label": name,
            "clientName": name,
            "clientIndex": ci,
            "sweeps": sweeps,
        })

    return {"strategies": strategies}


@app.post("/api/run")
async def run_from_file(req: RunConfigRequest):
    """Run a single experiment from a YAML config file."""
    config_path = YAML_DIR / req.config_path
    if not config_path.exists():
        raise HTTPException(404, f"Config not found: {req.config_path}")

    try:
        config = ConfigLoader.load_from_file(str(config_path))
        t0 = time.time()
        result = run_simulation(config)
        result["elapsed_s"] = round(time.time() - t0, 2)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Simulation error: {str(e)}")


@app.post("/api/run_inline")
async def run_inline(req: RunInlineRequest):
    """Run a single experiment from inline YAML content."""
    try:
        config = ConfigLoader.load_from_string(req.yaml_content)
        t0 = time.time()
        result = run_simulation(config)
        result["elapsed_s"] = round(time.time() - t0, 2)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Simulation error: {str(e)}")


@app.post("/api/sweep")
async def run_sweep(req: SweepRequest):
    """Run a parameter sweep over a single parameter."""
    import yaml
    config_path = YAML_DIR / req.config_path
    if not config_path.exists():
        raise HTTPException(404, f"Config not found: {req.config_path}")

    with open(config_path) as f:
        base_yaml = yaml.safe_load(f)

    results = []
    for val in req.values:
        # Deep-set the parameter
        yaml_copy = json.loads(json.dumps(base_yaml))  # deep copy
        _set_nested(yaml_copy, req.param_path, val)

        try:
            yaml_str = yaml.dump(yaml_copy)
            config = ConfigLoader.load_from_string(yaml_str)
            t0 = time.time()
            result = run_simulation(config)
            elapsed = round(time.time() - t0, 2)

            # Extract aggregate summary
            agg = _aggregate_clients(result["clients"])
            results.append({
                "param_value": val,
                "elapsed_s": elapsed,
                **agg,
                "per_client": [
                    {
                        "name": c["name"],
                        "success_rate": c["summary"]["success_rate"],
                        "p50": c["summary"]["p50"],
                        "p95": c["summary"]["p95"],
                        "p99": c["summary"]["p99"],
                        "retries_per_root": c["summary"]["retries_per_root"],
                        "goodput_rps": c["summary"].get("goodput_rps", 0),
                        "amplification_factor": c["summary"].get("amplification_factor", 1.0),
                        "retry_efficiency": c["summary"].get("retry_efficiency", 0),
                        "recovery_time_s": c["summary"].get("recovery_time_s"),
                    }
                    for c in result["clients"]
                ],
            })
        except Exception as e:
            results.append({
                "param_value": val,
                "error": str(e),
            })

    return {
        "config": req.config_path,
        "param_path": req.param_path,
        "results": results,
    }


@app.post("/api/scaling")
async def run_scaling(req: ScalingRequest):
    """Run a scaling study by varying client count while keeping total RPS constant."""
    import yaml
    config_path = YAML_DIR / req.config_path
    if not config_path.exists():
        raise HTTPException(404, f"Config not found: {req.config_path}")

    with open(config_path) as f:
        base_yaml = yaml.safe_load(f)

    results = []
    for n in req.client_counts:
        yaml_copy = json.loads(json.dumps(base_yaml))

        # Adjust: set replicas or duplicate clients
        if "clients" in yaml_copy and len(yaml_copy["clients"]) > 0:
            # Set replicas on first client and adjust RPS
            client = yaml_copy["clients"][0]
            client["replicas"] = n
            if "workload" in client:
                client["workload"]["base_rps"] = req.total_rps / n
        elif "workload" in yaml_copy:
            yaml_copy["workload"]["base_rps"] = req.total_rps / n

        try:
            yaml_str = yaml.dump(yaml_copy)
            config = ConfigLoader.load_from_string(yaml_str)
            t0 = time.time()
            result = run_simulation(config)
            elapsed = round(time.time() - t0, 2)

            agg = _aggregate_clients(result["clients"])
            per_client = [c["summary"]["success_rate"] for c in result["clients"]]

            # Compute fairness for this scaling point
            fairness = _compute_fairness_share_shift(result["clients"], config)

            results.append({
                "client_count": n,
                "elapsed_s": elapsed,
                **agg,
                "per_client_success_rates": per_client,
                "fairness_share_shift": fairness,
            })
        except Exception as e:
            traceback.print_exc()
            results.append({"client_count": n, "error": str(e)})

    return {
        "config": req.config_path,
        "total_rps": req.total_rps,
        "results": results,
    }


@app.post("/api/multi_client_sweep")
async def run_multi_client_sweep(req: MultiClientSweepRequest):
    """Run a 2D sweep over p_fail × replicas for a multi-client config.

    Discovers p_fail and replicas list values from the YAML, then runs
    every (p_fail, replicas) combination.  Returns per-strategy metrics
    grouped for easy plotting (X = failure rate, lines = strategy × replicas).
    """
    import yaml
    from itertools import product as cartesian

    config_path = YAML_DIR / req.config_path
    if not config_path.exists():
        raise HTTPException(404, f"Config not found: {req.config_path}")

    with open(config_path) as f:
        base_yaml = yaml.safe_load(f)

    # --- discover p_fail values -----------------------------------------------
    p_fail_values = req.p_fail_values
    if p_fail_values is None:
        # Auto-detect from first service's partial_failures
        for svc in base_yaml.get("services", []):
            for pf in svc.get("partial_failures", []):
                v = pf.get("p_fail")
                if isinstance(v, list):
                    p_fail_values = [float(x) for x in v]
                    break
            if p_fail_values:
                break
    if not p_fail_values:
        raise HTTPException(400, "No p_fail sweep values found in config or request")

    # --- discover replicas values ---------------------------------------------
    replicas_values = req.replicas_values
    if replicas_values is None:
        for cl in base_yaml.get("clients", []):
            v = cl.get("replicas")
            if isinstance(v, list):
                replicas_values = [int(x) for x in v]
                break
    if not replicas_values:
        replicas_values = [1]  # fallback: single replica

    # --- discover client names ------------------------------------------------
    client_names = [c["name"] for c in base_yaml.get("clients", [])]

    # --- run the grid ---------------------------------------------------------
    grid_results = []  # flat list of {p_fail, replicas, clients: [...]}
    total_runs = len(p_fail_values) * len(replicas_values)
    run_idx = 0

    for pf_val, rep_val in cartesian(p_fail_values, replicas_values):
        run_idx += 1
        yaml_copy = json.loads(json.dumps(base_yaml))

        # Set ALL services' p_fail to the scalar value
        for svc in yaml_copy.get("services", []):
            for pf_entry in svc.get("partial_failures", []):
                if "p_fail" in pf_entry:
                    pf_entry["p_fail"] = pf_val

        # Set ALL clients' replicas to the scalar value
        for cl in yaml_copy.get("clients", []):
            if "replicas" in cl or isinstance(cl.get("replicas"), list):
                cl["replicas"] = rep_val
            # Keep total RPS constant: divide by N replicas
            if req.fixed_total_rps is not None and "workload" in cl:
                cl["workload"]["base_rps"] = req.fixed_total_rps / rep_val

        try:
            yaml_str = yaml.dump(yaml_copy)
            config = ConfigLoader.load_from_string(yaml_str)
            t0 = time.time()
            result = run_simulation(config)
            elapsed = round(time.time() - t0, 2)

            per_client = []
            for c in result["clients"]:
                s = c["summary"]
                total = s["total"]
                total_attempts = total * (1 + s["retries_per_root"]) if total > 0 else 0
                per_client.append({
                    "name": c["name"],
                    "success_rate": s["success_rate"],
                    "goodput_rps": s.get("goodput_rps", 0),
                    "amplification_factor": s.get("amplification_factor", 1.0),
                    "retry_efficiency": s.get("retry_efficiency", 0),
                    "total_requests": total,
                    "total_attempts": round(total_attempts),
                    "p50": s["p50"],
                    "p95": s["p95"],
                    "p99": s["p99"],
                    "recovery_time_s": s.get("recovery_time_s"),
                })

            grid_results.append({
                "p_fail": pf_val,
                "replicas": rep_val,
                "elapsed_s": elapsed,
                "clients": per_client,
            })
        except Exception as e:
            traceback.print_exc()
            grid_results.append({
                "p_fail": pf_val,
                "replicas": rep_val,
                "error": str(e),
            })

    # --- aggregate replicas and reshape into per-strategy series ----------------
    # With replicas=N the simulator creates N clients named "strategy.0",
    # "strategy.1", etc.  We aggregate them back into a single data point per
    # (base_strategy, replicas, p_fail).

    def _base_name(name: str) -> str:
        """Strip trailing '.N' replica suffix → base strategy name."""
        if "." in name and name.rsplit(".", 1)[-1].isdigit():
            return name.rsplit(".", 1)[0]
        return name

    strategies = {}  # key = (base_name, replicas)
    for gr in grid_results:
        if "error" in gr:
            continue

        # Group this run's clients by base name
        from collections import defaultdict
        buckets = defaultdict(list)
        for cl in gr["clients"]:
            buckets[_base_name(cl["name"])].append(cl)

        for base, members in buckets.items():
            key = (base, gr["replicas"])
            if key not in strategies:
                strategies[key] = {
                    "client_name": base,
                    "replicas": gr["replicas"],
                    "points": [],
                }

            n = len(members)
            avg_sr = sum(m["success_rate"] for m in members) / n
            sum_goodput = sum(m["goodput_rps"] for m in members)
            sum_requests = sum(m["total_requests"] for m in members)
            sum_attempts = sum(m["total_attempts"] for m in members)
            avg_amp = sum_attempts / sum_requests if sum_requests > 0 else 1.0
            retry_effs = [m["retry_efficiency"] for m in members if m["total_requests"] > 0]
            avg_re = sum(retry_effs) / len(retry_effs) if retry_effs else 0
            max_p50 = max(m["p50"] for m in members)
            max_p95 = max(m["p95"] for m in members)
            max_p99 = max(m["p99"] for m in members)

            strategies[key]["points"].append({
                "p_fail": gr["p_fail"],
                "success_rate": avg_sr,
                "goodput_rps": round(sum_goodput, 2),
                "amplification_factor": round(avg_amp, 4),
                "retry_efficiency": round(avg_re, 4),
                "total_requests": sum_requests,
                "total_attempts": sum_attempts,
                "p50": max_p50,
                "p95": max_p95,
                "p99": max_p99,
            })

    # Compute load_pct for each series (relative to its p_fail=0 baseline)
    series_list = list(strategies.values())
    for series in series_list:
        pts = series["points"]
        # Sort by p_fail
        pts.sort(key=lambda p: p["p_fail"])
        # Baseline = first point (lowest p_fail)
        baseline_attempts = pts[0]["total_attempts"] if pts else 1
        for pt in pts:
            pt["load_pct"] = (
                100.0 * pt["total_attempts"] / baseline_attempts
                if baseline_attempts > 0 else 100.0
            )

    return {
        "config": req.config_path,
        "p_fail_values": p_fail_values,
        "replicas_values": replicas_values,
        "client_names": client_names,
        "total_runs": total_runs,
        "series": series_list,
    }


@app.post("/api/chain")
async def run_chain(req: ChainRequest):
    """Run chain depth study by varying the number of service hops."""
    import yaml
    config_path = YAML_DIR / req.config_path
    if not config_path.exists():
        raise HTTPException(404, f"Config not found: {req.config_path}")

    with open(config_path) as f:
        base_yaml = yaml.safe_load(f)

    results = []
    base_services = base_yaml.get("services", [])
    if len(base_services) == 0:
        raise HTTPException(400, "Config must have at least one service")

    # Use first service as template
    svc_template = base_services[0]

    for depth in req.depths:
        yaml_copy = json.loads(json.dumps(base_yaml))

        # Build chain: svc-0 → svc-1 → ... → svc-(depth-1)
        chain_services = []
        for i in range(depth):
            svc = json.loads(json.dumps(svc_template))
            svc["name"] = f"svc-{i}"
            if i < depth - 1:
                svc["dependencies"] = [{"service": f"svc-{i+1}"}]
            else:
                svc.pop("dependencies", None)
            chain_services.append(svc)

        yaml_copy["services"] = chain_services

        # Point clients at first service
        if "clients" in yaml_copy:
            for c in yaml_copy["clients"]:
                c["target_service"] = "svc-0"

        try:
            yaml_str = yaml.dump(yaml_copy)
            config = ConfigLoader.load_from_string(yaml_str)
            t0 = time.time()
            result = run_simulation(config)
            elapsed = round(time.time() - t0, 2)

            agg = _aggregate_clients(result["clients"])

            # Per-hop breakdown from service metrics
            per_hop = []
            for svc_m in result.get("services", []):
                if isinstance(svc_m, dict) and "service" in svc_m:
                    per_hop.append(svc_m)

            results.append({
                "depth": depth,
                "elapsed_s": elapsed,
                **agg,
                "per_hop": per_hop,
            })

        except Exception as e:
            traceback.print_exc()
            results.append({"depth": depth, "error": str(e)})

    return {
        "config": req.config_path,
        "results": results,
    }


@app.post("/api/heatmap")
async def run_heatmap(req: HeatmapRequest):
    """Run a 2D parameter sweep (grid of X × Y values)."""
    import yaml
    config_path = YAML_DIR / req.config_path
    if not config_path.exists():
        raise HTTPException(404, f"Config not found: {req.config_path}")

    with open(config_path) as f:
        base_yaml = yaml.safe_load(f)

    results = []
    for vy in req.values_y:
        for vx in req.values_x:
            yaml_copy = json.loads(json.dumps(base_yaml))
            _set_nested(yaml_copy, req.param_path_x, vx)
            _set_nested(yaml_copy, req.param_path_y, vy)

            try:
                yaml_str = yaml.dump(yaml_copy)
                config = ConfigLoader.load_from_string(yaml_str)
                t0 = time.time()
                result = run_simulation(config)
                elapsed = round(time.time() - t0, 2)
                agg = _aggregate_clients(result["clients"])
                results.append({
                    "x": vx, "y": vy,
                    "elapsed_s": elapsed,
                    **agg,
                    "per_client": [
                        {
                            "name": c["name"],
                            "success_rate": c["summary"]["success_rate"],
                            "p50": c["summary"]["p50"],
                            "p95": c["summary"]["p95"],
                            "p99": c["summary"]["p99"],
                            "retries_per_root": c["summary"]["retries_per_root"],
                            "goodput_rps": c["summary"].get("goodput_rps", 0),
                            "amplification_factor": c["summary"].get("amplification_factor", 1.0),
                            "retry_efficiency": c["summary"].get("retry_efficiency", 0),
                            "recovery_time_s": c["summary"].get("recovery_time_s"),
                        }
                        for c in result["clients"]
                    ],
                })
            except Exception as e:
                traceback.print_exc()
                results.append({"x": vx, "y": vy, "error": str(e)})

    return {
        "config": req.config_path,
        "param_x": req.param_path_x,
        "param_y": req.param_path_y,
        "results": results,
    }


@app.post("/api/param_compare")
async def run_param_compare(req: ParamCompareRequest):
    """Run simulations varying a shared parameter across all strategies.

    Metrics are computed over the **fault phase only** (the time window covered
    by partial_failures / latency_spikes) so that the healthy steady-state
    period does not dilute the comparison.  Falls back to whole-simulation
    summary when no fault events are present.
    """
    import yaml
    config_path = YAML_DIR / req.config_path
    if not config_path.exists():
        raise HTTPException(404, f"Config not found: {req.config_path}")

    with open(config_path) as f:
        base_yaml = yaml.safe_load(f)

    # Expand short param name to all matching dot-paths
    param_paths = _expand_broadcast_param(base_yaml, req.param_name)
    if not param_paths:
        raise HTTPException(400, f"No matching paths found for parameter '{req.param_name}'")

    results = []
    for val in req.values:
        yaml_copy = json.loads(json.dumps(base_yaml))
        for p in param_paths:
            _set_nested(yaml_copy, p, val)

        try:
            yaml_str = yaml.dump(yaml_copy)
            config = ConfigLoader.load_from_string(yaml_str)
            t0 = time.time()
            result = run_simulation(config)
            elapsed = round(time.time() - t0, 2)

            fault_events = result.get("fault_events", [])
            per_client = _extract_fault_phase_metrics(result["clients"], fault_events)

            results.append({
                "param_value": val,
                "elapsed_s": elapsed,
                "per_client": per_client,
            })
        except Exception as e:
            traceback.print_exc()
            results.append({"param_value": val, "error": str(e)})

    return {
        "config": req.config_path,
        "param_name": req.param_name,
        "param_paths": param_paths,
        "results": results,
    }


# ============================================================
# Helpers
# ============================================================

def _extract_fault_phase_metrics(
    clients: list, fault_events: list,
) -> list[dict]:
    """Compute per-client metrics restricted to the fault window.

    Uses timeseries buckets where ``fault_start <= timepoint < fault_end``.
    Falls back to whole-simulation summary when no fault events exist.
    """
    # Determine fault window
    fault_start = fault_end = None
    if fault_events:
        fault_start = min(
            fe["start_time_s"] for fe in fault_events if "start_time_s" in fe
        )
        fault_end = max(
            fe["end_time_s"] for fe in fault_events if "end_time_s" in fe
        )

    per_client = []
    for c in clients:
        ts = c.get("timeseries", [])

        if fault_start is not None and fault_end is not None and ts:
            buckets = [
                b for b in ts
                if fault_start <= b["timepoint"] < fault_end
            ]
        else:
            buckets = ts  # no fault → use everything

        if not buckets:
            # Fallback to whole-sim summary
            s = c["summary"]
            per_client.append({
                "name": c["name"],
                "success_rate": s["success_rate"],
                "goodput_rps": s.get("goodput_rps", 0),
                "amplification_factor": s.get("amplification_factor", 1.0),
                "retry_efficiency": s.get("retry_efficiency", 0),
                "p50": s.get("p50", 0),
                "p95": s.get("p95", 0),
                "p99": s.get("p99", 0),
                "recovery_time_s": s.get("recovery_time_s"),
            })
            continue

        # Aggregate timeseries buckets within the fault window
        total_roots = sum(b.get("root_requests", 0) for b in buckets)
        total_success = sum(b.get("success_root", 0) for b in buckets)
        total_retries = sum(b.get("retries", 0) for b in buckets)
        total_retry_fails = sum(b.get("failure_retry", 0) for b in buckets)
        total_attempts = total_roots + total_retries

        success_rate = total_success / total_roots if total_roots > 0 else 0
        n_buckets = len(buckets)
        goodput_rps = sum(b.get("goodput_rps", 0) for b in buckets) / n_buckets
        amp = total_attempts / total_roots if total_roots > 0 else 1.0
        retry_eff = (
            (total_retries - total_retry_fails) / total_retries
            if total_retries > 0 else 0
        )

        # p99 during fault: use max of per-bucket p99s as conservative estimate
        p50_vals = [b.get("p50", 0) for b in buckets if b.get("p50", 0) > 0]
        p95_vals = [b.get("p95", 0) for b in buckets if b.get("p95", 0) > 0]
        p99_vals = [b.get("p99", 0) for b in buckets if b.get("p99", 0) > 0]

        per_client.append({
            "name": c["name"],
            "success_rate": round(success_rate, 4),
            "goodput_rps": round(goodput_rps, 2),
            "amplification_factor": round(amp, 3),
            "retry_efficiency": round(retry_eff, 3),
            "p50": round(max(p50_vals), 2) if p50_vals else 0,
            "p95": round(max(p95_vals), 2) if p95_vals else 0,
            "p99": round(max(p99_vals), 2) if p99_vals else 0,
            "recovery_time_s": c["summary"].get("recovery_time_s"),
        })

    return per_client


def _expand_broadcast_param(yaml_dict: dict, param_name: str) -> list[str]:
    """Expand a short param name to all matching dot-paths in the config."""
    paths = []
    if param_name == "p_fail":
        for i, svc in enumerate(yaml_dict.get("services", [])):
            for j, pf in enumerate(svc.get("partial_failures", [])):
                if "p_fail" in pf:
                    paths.append(f"services.{i}.partial_failures.{j}.p_fail")
    elif param_name == "base_rps":
        for i, c in enumerate(yaml_dict.get("clients", [])):
            wl = c.get("workload")
            if wl and "base_rps" in wl:
                paths.append(f"clients.{i}.workload.base_rps")
    elif param_name == "workers":
        for i, svc in enumerate(yaml_dict.get("services", [])):
            if "workers" in svc:
                paths.append(f"services.{i}.workers")
    elif param_name == "max_attempts":
        for i, c in enumerate(yaml_dict.get("clients", [])):
            retry = c.get("retry")
            if retry and "max_attempts" in retry:
                paths.append(f"clients.{i}.retry.max_attempts")
    elif param_name == "attempt_timeout_ms":
        for i, c in enumerate(yaml_dict.get("clients", [])):
            to = c.get("timeout")
            if to and "attempt_ms" in to:
                paths.append(f"clients.{i}.timeout.attempt_ms")
    elif param_name == "queue_capacity":
        for i, svc in enumerate(yaml_dict.get("services", [])):
            if "queue_capacity" in svc:
                paths.append(f"services.{i}.queue_capacity")
    return paths

def _default_sweep_range(param_name: str, current_value) -> list:
    """Generate sensible default sweep values based on parameter name and current value."""
    if current_value is None:
        return []
    v = float(current_value)

    # Ratio parameters (0-1 range)
    if param_name in ("alpha", "beta_down", "beta_up", "budget_ratio",
                       "failure_threshold", "max_retry_ratio",
                       "success_rate_threshold", "success_rate_beta"):
        return [0.01, 0.05, 0.1, 0.2, 0.3, 0.5]

    # Integer count parameters
    if param_name in ("max_attempts",):
        return [1, 2, 3, 4, 5, 6, 8]
    if param_name in ("max_retries",):
        return [1, 5, 10, 20, 50, 100]
    if param_name in ("min_requests",):
        return [5, 10, 20, 50, 100]
    if param_name in ("min_retries_per_sec",):
        return [0, 1, 5, 10, 20, 50]

    # Time/delay parameters (ms)
    if param_name.endswith("_ms"):
        if v <= 0:
            return [0, 50, 100, 200, 500, 1000]
        # Geometric progression around current value
        factors = [0.25, 0.5, 1.0, 2.0, 5.0, 10.0]
        vals = sorted(set(round(v * f) for f in factors))
        return [x for x in vals if x >= 0]

    # Fallback: linear spread around current value
    if v == 0:
        return [0, 1, 2, 5, 10, 20]
    factors = [0.25, 0.5, 1.0, 2.0, 4.0]
    return sorted(set(round(v * f, 4) for f in factors))


def _set_nested(d: dict, path: str, value):
    """Set a nested dict value by dot-separated path. Supports array indexing like 'clients.0.retry.max_attempts'."""
    keys = path.split(".")
    for key in keys[:-1]:
        if key.isdigit():
            d = d[int(key)]
        else:
            d = d[key]
    final_key = keys[-1]
    if final_key.isdigit():
        d[int(final_key)] = value
    else:
        d[final_key] = value


def _aggregate_clients(clients: list) -> dict:
    """Aggregate per-client summaries into a single result."""
    total_requests = sum(c["summary"]["total"] for c in clients)
    total_succeeded = sum(c["summary"]["succeeded"] for c in clients)
    total_retries = sum(c["summary"]["total"] * c["summary"]["retries_per_root"] for c in clients)

    p50_values = [c["summary"]["p50"] for c in clients if c["summary"]["total"] > 0]
    p95_values = [c["summary"]["p95"] for c in clients if c["summary"]["total"] > 0]
    p99_values = [c["summary"]["p99"] for c in clients if c["summary"]["total"] > 0]
    max_p50 = max(p50_values) if p50_values else 0
    max_p95 = max(p95_values) if p95_values else 0
    max_p99 = max(p99_values) if p99_values else 0

    # Aggregate goodput and retry efficiency
    total_goodput = sum(c["summary"].get("goodput_rps", 0) for c in clients)
    retry_effs = [c["summary"].get("retry_efficiency", 0) for c in clients if c["summary"]["total"] > 0]
    avg_retry_eff = sum(retry_effs) / len(retry_effs) if retry_effs else 0

    return {
        "success_rate": total_succeeded / total_requests if total_requests > 0 else 0,
        "total_requests": total_requests,
        "total_succeeded": total_succeeded,
        "amplification": (total_requests + total_retries) / total_requests if total_requests > 0 else 1,
        "goodput_rps": round(total_goodput, 2),
        "retry_efficiency": round(avg_retry_eff, 3),
        "p50": max_p50,
        "p95": max_p95,
        "p99": max_p99,
    }


# ============================================================
# Results saving
# ============================================================


class SaveResultsRequest(BaseModel):
    analysis_type: str = Field(description="Type: timeseries, sweep, tornado, heatmap")
    label: str = Field(default="", description="Optional label for the result")
    data: dict = Field(description="The result data to save")


def _save_result(analysis_type: str, data: dict, label: str = "") -> str:
    """Save result JSON to the results directory. Returns the file path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = f"_{label}" if label else ""
    folder_name = f"viz_{analysis_type}{slug}_{ts}"
    out_dir = RESULTS_DIR / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{analysis_type}.json"
    with open(out_file, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"[save] {out_file}")
    return str(out_file)


@app.post("/api/save_results")
async def save_results(req: SaveResultsRequest):
    """Save analysis results to the results directory."""
    try:
        path = _save_result(req.analysis_type, req.data, req.label)
        return {"saved": True, "path": path}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Save error: {str(e)}")


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    import uvicorn
    print("Starting Retry Budget Simulator API on http://localhost:8642")
    print(f"YAML configs dir: {YAML_DIR}")
    print("Docs at: http://localhost:8642/docs")
    uvicorn.run(app, host="0.0.0.0", port=8642)
