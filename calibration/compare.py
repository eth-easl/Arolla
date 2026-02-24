#!/usr/bin/env python3
"""
Compare simulator output against observed prototype metrics.

Reads:
  - fitted_params.json              (observed P50/P99 from the prototype)
  - data/sim_output/service_metrics.csv  (simulated latencies from run_experiment.py)

Computes mean simulated P50/P99 across all time buckets per service,
prints a comparison table, writes a text report, and optionally generates
a latency-over-time plot (PNG) per service.

Usage:
    python3 compare.py data/fitted_params.json data/sim_output/service_metrics.csv
    python3 compare.py data/fitted_params.json data/sim_output/service_metrics.csv \\
        --report reports/calibration_report.txt --plot-dir reports/
"""

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ms(v: float | None) -> str:
    return f"{v:8.1f}ms" if v is not None else f"{'N/A':>9}  "


def _fmt_err(v: float | None) -> str:
    if v is None:
        return f"{'N/A':>7}  "
    marker = " !" if v > 25 else "  "
    return f"{v:6.1f}%{marker}"


# ── Core comparison ───────────────────────────────────────────────────────────

def _aggregate_exact_from_attempts(attempts_df: "pd.DataFrame") -> dict[str, dict]:
    """Compute exact per-service percentiles from raw attempt events (success-only)."""
    if attempts_df.empty:
        return {}

    required = {"service", "latency_ms", "success"}
    if not required.issubset(set(attempts_df.columns)):
        missing = sorted(required - set(attempts_df.columns))
        raise ValueError(f"service_attempts.csv missing columns: {missing}")

    success_series = attempts_df["success"]
    if success_series.dtype != bool:
        success_norm = success_series.astype(str).str.lower().map({
            "true": True,
            "false": False,
            "1": True,
            "0": False,
        })
    else:
        success_norm = success_series

    sim_agg: dict[str, dict] = {}
    for svc, grp in attempts_df.groupby("service"):
        succ_mask = success_norm.loc[grp.index] == True
        succ = grp.loc[succ_mask, "latency_ms"].dropna()
        if succ.empty:
            sim_agg[svc] = {"p50": None, "p99": None}
            continue

        p50 = float(succ.quantile(0.50))
        p99 = float(succ.quantile(0.99))
        sim_agg[svc] = {"p50": p50, "p99": p99}
    return sim_agg


def _aggregate_fallback_from_bucket_metrics(sim_df: "pd.DataFrame") -> dict[str, dict]:
    """
    Backward-compatible fallback when raw attempt events are unavailable.

    This is an approximation (weighted quantile over bucket quantiles), not an
    exact global percentile.
    """
    sim_agg: dict[str, dict] = {}
    for svc, grp in sim_df.groupby("service"):
        nonempty = grp[grp["total_requests"] > 0].copy()
        if nonempty.empty:
            sim_agg[svc] = {"p50": None, "p99": None}
            continue
        weights = nonempty["total_requests"].values

        def _weighted_quantile(values, weights, q: float) -> float:
            order = values.argsort()
            vals_sorted = values[order]
            wts_sorted = weights[order]
            cumw = wts_sorted.cumsum()
            threshold = q * cumw[-1]
            idx = (cumw >= threshold).argmax()
            return float(vals_sorted[idx])

        sim_agg[svc] = {
            "p50": _weighted_quantile(nonempty["p50"].values, weights, 0.50),
            "p99": _weighted_quantile(nonempty["p99"].values, weights, 0.99),
        }
    return sim_agg


def compare(
    fitted: dict,
    sim_df: "pd.DataFrame",
    attempts_df: "pd.DataFrame | None" = None,
    report_path: str | None = None,
) -> tuple[int, list[dict]]:
    """Compare observed vs simulated latency percentiles per service.

    Args:
        fitted:      Parsed fitted_params.json dict.
        sim_df:      service_metrics.csv DataFrame from run_experiment.py.
        report_path: Optional file path to write the text report.

    Returns:
        (n_warnings, rows) where n_warnings is the count of services with
        P99 error > 25% and rows is the per-service comparison data.
    """
    if "service" not in sim_df.columns or "p50" not in sim_df.columns:
        print(
            "Error: sim_metrics DataFrame missing 'service' or 'p50' columns.\n"
            "Expected: service_metrics.csv from run_experiment.py (multi-service output).",
            file=sys.stderr,
        )
        return 0, []

    used_exact_attempts = attempts_df is not None and not attempts_df.empty
    if used_exact_attempts:
        sim_agg = _aggregate_exact_from_attempts(attempts_df)
    else:
        print(
            "  [WARN] service_attempts.csv not found; falling back to approximate "
            "comparison from bucketed service_metrics.csv",
            file=sys.stderr,
        )
        sim_agg = _aggregate_fallback_from_bucket_metrics(sim_df)

    rows = []
    warnings = []

    for svc_name, obs in fitted["services"].items():
        # Skip TCP fallback services (no observed latency histogram)
        if obs.get("tcp_only") or obs.get("p50_ms") is None:
            continue

        obs_p50: float = obs["p50_ms"]
        obs_p99: float | None = obs.get("p99_ms")

        sim = sim_agg.get(svc_name, {})
        sim_p50: float | None = sim.get("p50")
        sim_p99: float | None = sim.get("p99")

        p50_err = (
            abs(sim_p50 - obs_p50) / obs_p50 * 100
            if sim_p50 is not None and obs_p50 > 0
            else None
        )
        p99_err = (
            abs(sim_p99 - obs_p99) / obs_p99 * 100
            if sim_p99 is not None and obs_p99 is not None and obs_p99 > 0
            else None
        )

        rows.append({
            "service": svc_name,
            "obs_p50": obs_p50,
            "sim_p50": sim_p50,
            "p50_err": p50_err,
            "obs_p99": obs_p99,
            "sim_p99": sim_p99,
            "p99_err": p99_err,
        })

        if p99_err is not None and p99_err > 25:
            warnings.append(
                f"  [WARN] {svc_name}: P99 error {p99_err:.1f}% exceeds 25% threshold"
            )

    # ── Build report text ──────────────────────────────────────────────────────
    col_svc = 28
    header = (
        f"{'Service':<{col_svc}}"
        f" {'Obs P50':>10} {'Sim P50':>10} {'P50 Err':>9}"
        f"   {'Obs P99':>10} {'Sim P99':>10} {'P99 Err':>9}"
    )
    sep = "-" * len(header)

    lines = [
        "Calibration Comparison Report",
        "",
        header,
        sep,
    ]

    for r in rows:
        line = (
            f"{r['service']:<{col_svc}}"
            f" {_fmt_ms(r['obs_p50'])} {_fmt_ms(r['sim_p50'])} {_fmt_err(r['p50_err'])}"
            f"   {_fmt_ms(r['obs_p99'])} {_fmt_ms(r['sim_p99'])} {_fmt_err(r['p99_err'])}"
        )
        lines.append(line)

    lines.append(sep)

    if not rows:
        lines.append("(no comparable services found)")
    elif not warnings:
        lines.append("All services within 25% P99 threshold.")
    else:
        lines.append("")
        lines.append(f"{len(warnings)} service(s) exceed the 25% P99 threshold:")
        lines.extend(warnings)
        lines.append("")
        lines.append(
            "Possible causes: low traffic during collection (increase --wait),\n"
            "  workers underestimated (adjust estimate_workers() in fit.py),\n"
            "  or dependency_call_pattern mismatch (try 'parallel' for frontend)."
        )

    lines.append("")
    lines.append(
        "Comparison basis: observed=Envoy response_code.200 (success-only attempts); "
        f"simulated={'exact success-only per-attempt percentiles' if used_exact_attempts else 'approximate bucketed percentiles'}."
    )

    report_text = "\n".join(lines) + "\n"
    print(report_text)

    if report_path:
        report_out = Path(report_path)
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(report_text)
        print(f"Report written to {report_path}")

    return len(warnings), rows


# ── Latency-over-time plot ────────────────────────────────────────────────────

def plot_latency_over_time(
    sim_df: "pd.DataFrame",
    fitted: dict,
    rows: list[dict],
    plot_dir: Path,
) -> None:
    """Generate a latency-over-time PNG comparing simulated vs observed P50/P99.

    For each service that has both observed histogram data and simulator time-
    series output, one subplot is drawn showing:
      - Simulated P50(t) and P99(t) as solid coloured lines
      - Observed P50 and P99 as horizontal dashed reference lines

    The combined figure is saved to <plot_dir>/latency_comparison.png.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  [WARN] matplotlib not installed — skipping plots (pip install matplotlib)")
        return

    # Filter to services present in both observed and simulated data
    sim_services = set(sim_df["service"].unique())
    comparable = [r for r in rows if r["sim_p50"] is not None and r["service"] in sim_services]

    if not comparable:
        print("  [WARN] No comparable services found for plotting.")
        return

    n = len(comparable)
    ncols = min(3, n)
    nrows = math.ceil(n / ncols)

    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(6 * ncols, 4 * nrows),
        squeeze=False,
    )
    fig.suptitle("Simulated vs Observed Latency Over Time", fontsize=13, fontweight="bold")

    for idx, r in enumerate(comparable):
        ax = axes[idx // ncols][idx % ncols]
        svc = r["service"]

        svc_df = sim_df[sim_df["service"] == svc].sort_values("timepoint")
        t = svc_df["timepoint"].values

        # Simulated time series
        ax.plot(t, svc_df["p50"].values, color="steelblue",
                linewidth=1.8, label="Sim P50")
        ax.plot(t, svc_df["p99"].values, color="darkorange",
                linewidth=1.8, label="Sim P99")

        # Observed reference lines
        if r["obs_p50"] is not None:
            ax.axhline(r["obs_p50"], color="steelblue", linestyle="--",
                       linewidth=1.2, alpha=0.75,
                       label=f"Obs P50 = {r['obs_p50']:.1f} ms")
        if r["obs_p99"] is not None:
            ax.axhline(r["obs_p99"], color="darkorange", linestyle="--",
                       linewidth=1.2, alpha=0.75,
                       label=f"Obs P99 = {r['obs_p99']:.1f} ms")

        # Error annotation in top-right corner
        err_parts = []
        if r["p50_err"] is not None:
            err_parts.append(f"P50 err {r['p50_err']:.0f}%")
        if r["p99_err"] is not None:
            marker = " !" if r["p99_err"] > 25 else ""
            err_parts.append(f"P99 err {r['p99_err']:.0f}%{marker}")
        if err_parts:
            ax.text(0.98, 0.97, "\n".join(err_parts),
                    transform=ax.transAxes, fontsize=7.5,
                    verticalalignment="top", horizontalalignment="right",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="lightyellow",
                              edgecolor="gray", alpha=0.8))

        ax.set_title(svc, fontsize=10, fontweight="bold")
        ax.set_xlabel("Time (s)", fontsize=9)
        ax.set_ylabel("Latency (ms)", fontsize=9)
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(True, alpha=0.25, linestyle=":")
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)

    # Hide unused subplot axes
    for idx in range(n, nrows * ncols):
        axes[idx // ncols][idx % ncols].set_visible(False)

    plt.tight_layout(rect=[0, 0, 1, 0.97])

    plot_dir.mkdir(parents=True, exist_ok=True)
    out_path = plot_dir / "latency_comparison.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Plot saved → {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare simulator output vs observed prototype metrics"
    )
    parser.add_argument(
        "fitted_params",
        help="Path to fitted_params.json (from fit.py)"
    )
    parser.add_argument(
        "sim_metrics",
        help="Path to service_metrics.csv (from run_experiment.py)"
    )
    parser.add_argument(
        "--report", default="reports/calibration_report.txt",
        help="Output report file (default: reports/calibration_report.txt)"
    )
    parser.add_argument(
        "--plot-dir", default=None,
        help="Directory to save latency-over-time plot PNG (e.g. reports/)"
    )
    args = parser.parse_args()

    try:
        fitted = json.loads(Path(args.fitted_params).read_text())
        sim_df = pd.read_csv(args.sim_metrics)
        attempts_path = Path(args.sim_metrics).with_name("service_attempts.csv")
        if attempts_path.exists():
            try:
                attempts_df = pd.read_csv(attempts_path)
            except pd.errors.EmptyDataError:
                attempts_df = None
        else:
            attempts_df = None
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except pd.errors.EmptyDataError:
        print(
            f"Error: {args.sim_metrics} is empty. "
            "Run the simulator first with RUN_SIM=1 ./pipeline.sh",
            file=sys.stderr,
        )
        return 1

    n_warnings, rows = compare(fitted, sim_df, attempts_df=attempts_df, report_path=args.report)

    if args.plot_dir:
        plot_latency_over_time(sim_df, fitted, rows, Path(args.plot_dir))

    # Exit 1 if any service exceeds threshold (useful for CI checks)
    return 1 if n_warnings > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
