#!/usr/bin/env python3
"""
Fit simulator parameters from raw Envoy sidecar stats files.

For each service, parses the /stats dump collected by collect.sh and produces:
  - median_ms, lognorm_sigma  (lognormal latency distribution fit)
  - workers                   (concurrency estimate via Little's Law)
  - rps                       (request rate from counter delta)

Usage:
    python3 fit.py <raw_stats_dir> [--out fitted_params.json]
"""

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.stats import norm

# Import service topology constants
sys.path.insert(0, str(Path(__file__).parent))
from topology import SERVICE_PORTS, TCP_SERVICES


# ── Envoy stat parsing ────────────────────────────────────────────────────────

def _parse_metadata(text: str) -> dict:
    """Extract # KEY=VALUE pairs from the leading comment line."""
    meta = {}
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        for key, val in re.findall(r'(\w+)=(\S+)', line):
            meta[key] = val
    return meta


def _find_istio_duration_lines(stats_text: str, svc_name: str) -> list[str]:
    """Find all Istio request duration histogram lines for this destination (200 only).

    Istio sidecars emit stats in the form:
        istiocustom.istio_request_duration_milliseconds.reporter.destination.
            ...destination_workload.<SVC>....response_code.200...:
            P0(interval,cumulative) P25(...) ... P99(...) P100(...)
    """
    svc_key = f"destination_workload.{svc_name}."
    lines = []
    for line in stats_text.splitlines():
        if (line.startswith("istiocustom.istio_request_duration_milliseconds.reporter.destination.")
                and svc_key in line
                and "response_code.200." in line):
            lines.append(line)
    return lines


def _non_nan_interval_count(line: str) -> int:
    """Count how many interval values (first in each Pnn pair) are non-nan."""
    count = 0
    for m in re.finditer(r'P\d+(?:\.\d+)?\(([^,]+),', line):
        try:
            if not math.isnan(float(m.group(1))):
                count += 1
        except ValueError:
            pass
    return count


def parse_histogram(stats_text: str, svc_name: str, port: int) -> dict[float, float] | None:
    """Parse Envoy HdrHistogram percentiles from Istio custom stats.

    Istio sidecar format (reporter=destination, response_code=200):
        istiocustom.istio_request_duration_milliseconds.reporter.destination.
            ...destination_workload.<SVC>....response_code.200...:
            P0(interval_val,cumulative_val) P25(...) P50(...) P75(...)
            P90(...) P95(...) P99(...) P99.5(...) P99.9(...) P100(...)

    Selects the histogram line with the most non-nan interval values (i.e.
    the line with the most recent in-window traffic).  Prefers the interval
    value (first) over the cumulative (second) for each percentile because
    the interval value reflects the measurement window after reset_counters.

    Returns: {percentile_float: value_ms, ...} or None if not found.
    """
    lines = _find_istio_duration_lines(stats_text, svc_name)
    if not lines:
        return None

    # Pick the line with the most non-nan interval values (most recent traffic)
    best_line = max(lines, key=_non_nan_interval_count)

    pct_map: dict[float, float] = {}
    for m in re.finditer(r'P(\d+(?:\.\d+)?)\(([^,]+),([^)]+)\)', best_line):
        pct_label = float(m.group(1))
        interval_str = m.group(2).strip()
        cumulative_str = m.group(3).strip()

        # Prefer interval value (reflects measurement window); fall back to cumulative
        val: float | None = None
        try:
            iv = float(interval_str)
            if not math.isnan(iv) and iv > 0:
                val = iv
        except ValueError:
            pass

        if val is None:
            try:
                cv = float(cumulative_str)
                if not math.isnan(cv) and cv > 0:
                    val = cv
            except ValueError:
                pass

        if val is not None:
            pct_map[pct_label] = val

    return pct_map if pct_map else None


def parse_inbound_counter(stats_text: str, port: int, suffix: str) -> int:
    """Extract a counter/gauge value from the inbound cluster stat.

    Istio format: cluster.inbound|<port>||;.<suffix>: <value>
    """
    prefix = f"cluster.inbound|{port}||;.{suffix}:"
    for line in stats_text.splitlines():
        if line.startswith(prefix):
            val_str = line.split(":", 1)[1].strip()
            try:
                return int(val_str)
            except ValueError:
                pass
    return 0


# ── Lognormal fitting ─────────────────────────────────────────────────────────

def fit_lognormal(pct_map: dict[float, float]) -> tuple[float, float]:
    """Fit a lognormal(mu, sigma) to the observed percentile map.

    Strategy:
      - Pin mu = log(P50)  (most robust anchor)
      - Fit sigma by least-squares over remaining percentiles via scipy

    Returns (median_ms, lognorm_sigma).
    """
    p50 = pct_map.get(50.0)
    if p50 is None or p50 <= 0:
        return 1.0, 0.5  # fallback: 1ms median, moderate spread

    mu = math.log(p50)

    # Collect (quantile, observed_ms) pairs excluding the P50 anchor
    pairs = [
        (q / 100.0, v)
        for q, v in sorted(pct_map.items())
        if q != 50.0 and v > 0 and 0 < q / 100.0 < 1
    ]

    if len(pairs) < 2:
        # Fall back to closed-form two-point from P99
        p99 = pct_map.get(99.0)
        if p99 and p99 > p50:
            sigma = (math.log(p99) - mu) / norm.ppf(0.99)
            return p50, max(0.01, sigma)
        return p50, 0.5

    # Least-squares: minimize sum of squared log-errors over available percentiles
    def residual(sigma: float) -> float:
        if sigma <= 0:
            return 1e10
        total = 0.0
        for q, x_obs in pairs:
            x_fit = math.exp(mu + sigma * float(norm.ppf(q)))
            if x_fit > 0 and x_obs > 0:
                total += (math.log(x_obs) - math.log(x_fit)) ** 2
        return total

    result = minimize_scalar(residual, bounds=(0.001, 5.0), method='bounded')
    sigma = float(result.x)

    return p50, max(0.01, sigma)


# ── Worker estimation ─────────────────────────────────────────────────────────

def estimate_workers(rps: float, p50_ms: float, rq_active: int) -> int:
    """Estimate worker count via Little's Law + observed gauge, with 2x headroom.

    L = λW  →  expected_concurrency = rps * (p50_ms / 1000)

    The 2x headroom prevents the simulator from being saturated at the
    calibrated load level, which would make the baseline simulation unrealistic.
    """
    workers_law = max(1, math.ceil(rps * (p50_ms / 1000.0))) if rps > 0 else 1
    workers_obs = max(1, rq_active)
    return max(4, workers_law, workers_obs) * 2


# ── Plausibility checks ───────────────────────────────────────────────────────

def check_plausibility(svc: str, median_ms: float, sigma: float,
                        workers: int, rps: float | None) -> None:
    if median_ms < 0.1:
        print(f"    [WARN] median_ms={median_ms:.3f} suspiciously small (parsing error?)")
    if median_ms > 10_000:
        print(f"    [WARN] median_ms={median_ms:.1f} suspiciously large (no traffic during warmup?)")
    if sigma > 3.0:
        print(f"    [WARN] lognorm_sigma={sigma:.2f} extremely heavy tail")
    if workers < 2:
        print(f"    [WARN] workers={workers} very low (no traffic?)")
    if rps is not None and rps < 0.1:
        print(f"    [WARN] rps={rps:.3f} very low (no traffic observed)")


# ── Per-service processing ────────────────────────────────────────────────────

def process_service(svc_name: str, stats_file: Path) -> dict:
    """Parse one raw stats file and return fitted parameter dict."""
    text = stats_file.read_text(errors='replace')
    meta = _parse_metadata(text)

    port = int(meta.get("PORT", SERVICE_PORTS.get(svc_name, 8080)))
    elapsed_secs = float(meta.get("ELAPSED_SECS", 60))

    # TCP-only service: no HTTP histogram available
    if svc_name in TCP_SERVICES:
        return {
            "port": port,
            "elapsed_secs": elapsed_secs,
            "median_ms": 1.0,
            "lognorm_sigma": 0.1,
            "workers": 8,
            "rps": None,
            "p50_ms": None,
            "p99_ms": None,
            "rq_total": None,
            "rq_active": 0,
            "fit_method": "fallback",
            "tcp_only": True,
        }

    # HTTP / gRPC service
    pct_map = parse_histogram(text, svc_name, port)
    rq_total = parse_inbound_counter(text, port, "upstream_rq_total")
    rq_active = parse_inbound_counter(text, port, "upstream_rq_active")
    rps = rq_total / elapsed_secs if elapsed_secs > 0 else 0.0

    if pct_map:
        median_ms, sigma = fit_lognormal(pct_map)
        fit_method = "multi_percentile" if len(pct_map) >= 3 else "two_point"
    else:
        print(f"    [WARN] no histogram found — using fallback (10ms, sigma=0.5)")
        median_ms, sigma = 10.0, 0.5
        fit_method = "fallback"

    workers = estimate_workers(rps, median_ms, rq_active)
    check_plausibility(svc_name, median_ms, sigma, workers, rps)

    return {
        "port": port,
        "elapsed_secs": elapsed_secs,
        "median_ms": round(median_ms, 3),
        "lognorm_sigma": round(sigma, 4),
        "workers": workers,
        "rps": round(rps, 3),
        "p50_ms": pct_map.get(50.0) if pct_map else None,
        "p99_ms": pct_map.get(99.0) if pct_map else None,
        "rq_total": rq_total,
        "rq_active": rq_active,
        "fit_method": fit_method,
        "tcp_only": False,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fit simulator parameters from raw Envoy stats files"
    )
    parser.add_argument(
        "stats_dir",
        help="Directory containing <service>.txt files (output of collect.sh)"
    )
    parser.add_argument(
        "--out", default="fitted_params.json",
        help="Output JSON file (default: fitted_params.json)"
    )
    args = parser.parse_args()

    stats_dir = Path(args.stats_dir)
    if not stats_dir.exists():
        print(f"Error: stats_dir not found: {stats_dir}", file=sys.stderr)
        return 1

    output: dict = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "services": {},
    }

    found = 0
    for svc_name in SERVICE_PORTS:
        stats_file = stats_dir / f"{svc_name}.txt"
        if not stats_file.exists():
            print(f"  [SKIP] {svc_name}: no stats file")
            continue

        print(f"  Fitting {svc_name}...")
        try:
            params = process_service(svc_name, stats_file)
            output["services"][svc_name] = params

            # Summary line
            rps_str = f"  rps={params['rps']:.2f}" if params['rps'] is not None else ""
            tcp_str = "  [TCP fallback]" if params['tcp_only'] else ""
            print(
                f"    median_ms={params['median_ms']:.2f}"
                f"  sigma={params['lognorm_sigma']:.3f}"
                f"  workers={params['workers']}"
                f"{rps_str}{tcp_str}"
            )
            found += 1
        except Exception as exc:
            print(f"  [ERROR] {svc_name}: {exc}", file=sys.stderr)

    if found == 0:
        print("Error: no services processed — check stats_dir for .txt files", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Wrote {found} services to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
