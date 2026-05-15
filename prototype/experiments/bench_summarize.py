#!/usr/bin/env python3
"""Summarise rl-timings.jsonl + per-scenario summary.csv into markdown.

Crawls one phase output root (e.g. ``outputs/prototype/rb-rl-v3-bench/phase0``),
finds every ``rl-timings.jsonl`` under it (one per scenario × policy run), and
emits a markdown table with per-component p50 / p95 / p99 latencies aggregated
across all ticks of all scenarios. Also pulls outcome metrics
(``avg_goodput_fault``, ``avg_goodput_prefault``, ``recovery_sec``,
``amplification``) from each scenario's ``summary.csv`` so the reader can
verify behaviour didn't regress while latency dropped.

Usage::

    prototype/experiments/bench_summarize.py \\
        outputs/prototype/rb-rl-v3-bench/phase0 \\
        --out outputs/prototype/rb-rl-v3-bench/phase0/summary.md

Designed to be re-run after every phase. The output is the row that gets
appended to ``comparison.md``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

TIMING_COMPONENTS = (
    "obs_fetch_ms",
    "obs_parse_ms",
    "obs_build_ms",
    "inference_ms",
    "patch_ms",
    "total_ms",
)

OUTCOME_COLUMNS = (
    "avg_goodput_prefault",
    "avg_goodput_fault",
    "recovery_sec",
    "amplification",
    "retry_efficiency_pct",
)


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile, matching numpy default behaviour.

    Implemented inline so this script has no third-party deps and can run on
    a stock Python install (the RL venv has numpy but bench tooling shouldn't
    require it)."""
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    values = sorted(values)
    rank = (len(values) - 1) * pct / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[int(rank)]
    return values[lower] * (upper - rank) + values[upper] * (rank - lower)


def load_timings(jsonl_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def find_timings_files(root: Path) -> list[Path]:
    """All rl-timings.jsonl under root, sorted for stable output."""
    return sorted(root.rglob("rl-timings.jsonl"))


def scenario_label_from_path(timings_path: Path, root: Path) -> str:
    """The scenario+policy label: <scenario_dir>/<policy>.

    Layout: ``<root>/<scenario>/<policy>/rl-controller/rl-timings.jsonl``"""
    try:
        rel = timings_path.relative_to(root)
    except ValueError:
        return str(timings_path)
    parts = rel.parts
    if len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}"
    return "/".join(parts)


def summarise_component(rows: Iterable[dict[str, Any]], key: str) -> dict[str, float]:
    """p50/p95/p99 for one timing column, ignoring missing/None values."""
    vals = [float(r[key]) for r in rows if r.get(key) is not None]
    return {
        "n": len(vals),
        "p50": percentile(vals, 50.0),
        "p95": percentile(vals, 95.0),
        "p99": percentile(vals, 99.0),
        "mean": (sum(vals) / len(vals)) if vals else float("nan"),
    }


def load_summary_csv(summary_path: Path) -> dict[str, float]:
    """First (and usually only) row of summary.csv as a dict, floats parsed."""
    if not summary_path.is_file():
        return {}
    with summary_path.open() as f:
        reader = csv.DictReader(f)
        try:
            row = next(reader)
        except StopIteration:
            return {}
    out: dict[str, float] = {}
    for k, v in row.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def fmt_ms(value: float) -> str:
    if math.isnan(value):
        return "—"
    if value >= 1000:
        return f"{value/1000:.2f} s"
    return f"{value:.1f}"


def fmt_num(value: float) -> str:
    if math.isnan(value):
        return "—"
    if abs(value) >= 100:
        return f"{value:.1f}"
    return f"{value:.3f}"


def render_markdown(
    *,
    phase_root: Path,
    files: list[Path],
    aggregate_rows: list[dict[str, Any]],
    per_scenario: list[tuple[str, dict[str, dict[str, float]], dict[str, float]]],
) -> str:
    lines: list[str] = []
    lines.append(f"# Bench summary — {phase_root}")
    lines.append("")
    lines.append(
        f"_{len(files)} timing files, "
        f"{sum(len(load_timings(p)) for p in files)} total ticks aggregated._"
    )
    lines.append("")

    lines.append("## Aggregate per-component tick latency (ms)")
    lines.append("")
    lines.append("| component | n | p50 | p95 | p99 | mean |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for comp in TIMING_COMPONENTS:
        s = summarise_component(aggregate_rows, comp)
        lines.append(
            f"| {comp} | {s['n']} | {fmt_ms(s['p50'])} | {fmt_ms(s['p95'])} | "
            f"{fmt_ms(s['p99'])} | {fmt_ms(s['mean'])} |"
        )
    # xds_apply_ms is optional — only emit a row if any tick reported one.
    xds_rows = [r for r in aggregate_rows if r.get("xds_apply_ms") is not None]
    if xds_rows:
        s = summarise_component(xds_rows, "xds_apply_ms")
        lines.append(
            f"| xds_apply_ms | {s['n']} | {fmt_ms(s['p50'])} | {fmt_ms(s['p95'])} | "
            f"{fmt_ms(s['p99'])} | {fmt_ms(s['mean'])} |"
        )
    lines.append("")

    if per_scenario:
        lines.append("## Per-scenario breakdown")
        lines.append("")
        header = (
            "| scenario | ticks | total p50 | total p95 | obs_fetch p50 | "
            "patch p50 | inference p50 | "
            + " | ".join(OUTCOME_COLUMNS) + " |"
        )
        lines.append(header)
        lines.append(
            "|---|---:|---:|---:|---:|---:|---:|"
            + "|".join(["---:"] * len(OUTCOME_COLUMNS))
            + "|"
        )
        for label, comp_stats, outcome in per_scenario:
            n = comp_stats["total_ms"]["n"]
            lines.append(
                f"| {label} | {n} | "
                f"{fmt_ms(comp_stats['total_ms']['p50'])} | "
                f"{fmt_ms(comp_stats['total_ms']['p95'])} | "
                f"{fmt_ms(comp_stats['obs_fetch_ms']['p50'])} | "
                f"{fmt_ms(comp_stats['patch_ms']['p50'])} | "
                f"{fmt_ms(comp_stats['inference_ms']['p50'])} | "
                + " | ".join(fmt_num(outcome.get(c, float("nan"))) for c in OUTCOME_COLUMNS)
                + " |"
            )
        lines.append("")

    lines.append("## Sources")
    lines.append("")
    for p in files:
        lines.append(f"- `{p}`")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "phase_root",
        type=Path,
        help="Root directory (one phase) containing per-scenario subdirs.",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write markdown here (default: <phase_root>/summary.md). Pass '-' for stdout.",
    )
    args = parser.parse_args()

    root: Path = args.phase_root.resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    files = find_timings_files(root)
    if not files:
        print(f"warning: no rl-timings.jsonl under {root}", file=sys.stderr)

    aggregate_rows: list[dict[str, Any]] = []
    per_scenario: list[tuple[str, dict[str, dict[str, float]], dict[str, float]]] = []

    for path in files:
        rows = load_timings(path)
        aggregate_rows.extend(rows)
        comp_stats = {comp: summarise_component(rows, comp) for comp in TIMING_COMPONENTS}
        # summary.csv lives at <root>/<scenario>/summary.csv (per
        # run_rl.sh's per-cell layout). The timings file is two levels
        # deeper at <scenario>/<policy>/rl-controller/rl-timings.jsonl.
        scenario_dir = path.parent.parent.parent
        outcome = load_summary_csv(scenario_dir / "summary.csv")
        per_scenario.append((scenario_label_from_path(path, root), comp_stats, outcome))

    md = render_markdown(
        phase_root=root,
        files=files,
        aggregate_rows=aggregate_rows,
        per_scenario=per_scenario,
    )

    if args.out is None:
        out_path = root / "summary.md"
    elif str(args.out) == "-":
        sys.stdout.write(md)
        return 0
    else:
        out_path = args.out

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
