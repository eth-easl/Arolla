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
      position: fixed; top: 12px; left: 16px; z-index: 1000;
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
                        "p99": c["summary"]["p99"],
                        "p95": c["summary"]["p95"],
                        "retries_per_root": c["summary"]["retries_per_root"],
                        "goodput_rps": c["summary"].get("goodput_rps", 0),
                        "retry_efficiency": c["summary"].get("retry_efficiency", 0),
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
                        {"name": c["name"], "success_rate": c["summary"]["success_rate"]}
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


# ============================================================
# Helpers
# ============================================================

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
