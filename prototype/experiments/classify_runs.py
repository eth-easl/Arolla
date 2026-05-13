#!/usr/bin/env python3
"""Classify prototype experiment runs as recovered, metastable, or ambiguous.

The analyzer's ``summary.csv`` is intentionally compact. This helper adds a
multi-metric label for sweep triage by combining:

- recovery_sec from summary.csv
- final recovery goodput vs pre-fault goodput
- final recovery success rate
- cooldown/client-tail latency returning near the pre-fault tail

It accepts either a single run directory or a sweep parent and recursively
finds every ``summary.csv`` below it.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


FINAL_WINDOW_SEC = 30.0
LATENCY_ABS_THRESHOLD_MS = 500.0
LATENCY_MULTIPLIER = 2.0
GOODPUT_RECOVERY_RATIO = 0.90
SUCCESS_RECOVERY_PCT = 95.0


@dataclass
class Classification:
    summary: Path
    policy: str
    label: str
    recovery_sec: float | None
    final_goodput_ratio: float
    final_success_pct: float
    prefault_p95_ms: float
    cooldown_p95_ms: float
    reason: str


def _read_summary(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _read_policy_attempts(run_dir: Path, policy: str) -> pd.DataFrame:
    metrics_dir = run_dir / policy / "client-metrics"
    frames = []
    for path in sorted(metrics_dir.glob("*.csv")):
        try:
            frames.append(pd.read_csv(path))
        except pd.errors.EmptyDataError:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "ok" in df.columns:
        df["ok"] = df["ok"].astype(str).str.lower().isin({"1", "true", "yes"})
    return df


def _read_timeline(run_dir: Path, policy: str) -> dict:
    path = run_dir / policy / "timeline.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _logical_final_attempts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "request_id" not in df.columns:
        return pd.DataFrame()
    return df.sort_values("timestamp").groupby("request_id", as_index=False).tail(1)


def _goodput(final: pd.DataFrame, start: float, end: float) -> float:
    if final.empty or end <= start:
        return 0.0
    window = final[(final["timestamp"] >= start) & (final["timestamp"] < end)]
    if window.empty:
        return 0.0
    return float(window["ok"].sum()) / (end - start)


def _success_pct(final: pd.DataFrame, start: float, end: float) -> float:
    if final.empty or end <= start:
        return 0.0
    window = final[(final["timestamp"] >= start) & (final["timestamp"] < end)]
    if window.empty:
        return 0.0
    return float(window["ok"].mean()) * 100.0


def _p95_attempt_latency(df: pd.DataFrame, start: float, end: float) -> float:
    if df.empty or "latency_s" not in df.columns or end <= start:
        return float("nan")
    window = df[(df["timestamp"] >= start) & (df["timestamp"] < end)]
    if window.empty:
        return float("nan")
    return float(window["latency_s"].quantile(0.95) * 1000.0)


def classify_policy(run_dir: Path, row: dict[str, str]) -> Classification:
    policy = row["policy"]
    recovery_sec = _float_or_none(row.get("recovery_sec"))
    df = _read_policy_attempts(run_dir, policy)
    timeline = _read_timeline(run_dir, policy)

    t_prefault_start = float(timeline.get("t_warmup_end", 0.0))
    t_fault_start = float(timeline.get("t_fault_start", timeline.get("t_prefault_end", 0.0)))
    t_fault_end = float(timeline.get("t_fault_end", 0.0))
    t_recovery_end = float(timeline.get("t_recovery_end", 0.0))
    t_cooldown_end = float(timeline.get("t_cooldown_end", t_recovery_end))

    final = _logical_final_attempts(df)
    final_start = max(t_fault_end, t_recovery_end - FINAL_WINDOW_SEC)

    prefault_goodput = _goodput(final, t_prefault_start, t_fault_start)
    final_goodput = _goodput(final, final_start, t_recovery_end)
    final_goodput_ratio = final_goodput / prefault_goodput if prefault_goodput > 0 else 0.0
    final_success_pct = _success_pct(final, final_start, t_recovery_end)

    prefault_p95_ms = _p95_attempt_latency(df, t_prefault_start, t_fault_start)
    cooldown_p95_ms = _p95_attempt_latency(df, t_recovery_end, t_cooldown_end)
    latency_limit = max(
        LATENCY_ABS_THRESHOLD_MS,
        prefault_p95_ms * LATENCY_MULTIPLIER if not math.isnan(prefault_p95_ms) else LATENCY_ABS_THRESHOLD_MS,
    )
    latency_recovered = math.isnan(cooldown_p95_ms) or cooldown_p95_ms <= latency_limit

    reasons = []
    if recovery_sec is None:
        reasons.append("no success-rate recovery")
    if final_goodput_ratio < GOODPUT_RECOVERY_RATIO:
        reasons.append(f"final goodput {final_goodput_ratio:.2f}x prefault")
    if final_success_pct < SUCCESS_RECOVERY_PCT:
        reasons.append(f"final success {final_success_pct:.1f}%")
    if not latency_recovered:
        reasons.append(f"cooldown p95 {cooldown_p95_ms:.0f}ms > {latency_limit:.0f}ms")

    hard_fail = recovery_sec is None or final_goodput_ratio < 0.50 or final_success_pct < 80.0
    clean_recovery = (
        recovery_sec is not None
        and final_goodput_ratio >= GOODPUT_RECOVERY_RATIO
        and final_success_pct >= SUCCESS_RECOVERY_PCT
        and latency_recovered
    )

    if clean_recovery:
        label = "recovered"
        reason = "all recovery criteria passed"
    elif hard_fail:
        label = "metastable"
        reason = "; ".join(reasons)
    else:
        label = "ambiguous"
        reason = "; ".join(reasons) if reasons else "mixed recovery signals"

    return Classification(
        summary=run_dir / "summary.csv",
        policy=policy,
        label=label,
        recovery_sec=recovery_sec,
        final_goodput_ratio=final_goodput_ratio,
        final_success_pct=final_success_pct,
        prefault_p95_ms=prefault_p95_ms,
        cooldown_p95_ms=cooldown_p95_ms,
        reason=reason,
    )


def find_summaries(paths: Iterable[Path]) -> list[Path]:
    summaries = []
    for path in paths:
        if path.is_file() and path.name == "summary.csv":
            summaries.append(path)
        elif path.is_dir():
            summaries.extend(path.rglob("summary.csv"))
    return sorted(set(summaries))


PREFERRED_POLICY_FOR_SYMBOL = "envoy-retry-budget"


def choose_policy_row_for_symbol(rows: list[dict[str, str]]) -> dict[str, str] | None:
    """One classification per scenario: prefer retry-budget policy, else first row."""
    if not rows:
        return None
    for row in rows:
        if row.get("policy") == PREFERRED_POLICY_FOR_SYMBOL:
            return row
    return rows[0]


def plot_symbol_for_label(label: str) -> str:
    """Viewer suffix: + recovered, - metastable, ~ unhealthy (ambiguous or unknown)."""
    if label == "recovered":
        return "+"
    if label == "metastable":
        return "-"
    return "~"


def write_classification_json(run_dir: Path, c: Classification) -> None:
    path = run_dir / "classification.json"
    payload = {
        "policy": c.policy,
        "label": c.label,
        "symbol": plot_symbol_for_label(c.label),
        "reason": c.reason,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Run dirs, sweep dirs, or summary.csv files")
    parser.add_argument("--csv", type=Path, help="Optional machine-readable output CSV")
    args = parser.parse_args()

    rows: list[Classification] = []
    for summary in find_summaries(args.paths):
        run_dir = summary.parent
        summary_rows = _read_summary(summary)
        if not summary_rows:
            continue
        # Experiment runs use analyzer summary.csv (policy column). Skip
        # ancillary files like rb-rl-v1/comparison-plots/summary.csv.
        if "policy" not in summary_rows[0]:
            continue
        chosen = choose_policy_row_for_symbol(summary_rows)
        for row in summary_rows:
            if not row.get("policy"):
                continue
            c = classify_policy(run_dir, row)
            rows.append(c)
            if (
                chosen is not None
                and row.get("policy") == chosen.get("policy")
            ):
                write_classification_json(run_dir, c)

    if not rows:
        print("No summary.csv files found.")
        return 1

    header = (
        f"{'label':<11} {'policy':<20} {'recovery':>8} {'goodput':>8} "
        f"{'success':>8} {'pre_p95':>8} {'cool_p95':>9}  run"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        recovery = "NaN" if row.recovery_sec is None else f"{row.recovery_sec:.0f}s"
        print(
            f"{row.label:<11} {row.policy:<20} {recovery:>8} "
            f"{row.final_goodput_ratio:>7.2f}x {row.final_success_pct:>7.1f}% "
            f"{row.prefault_p95_ms:>7.0f}ms {row.cooldown_p95_ms:>8.0f}ms  "
            f"{row.summary.parent}"
        )
        print(f"{'':<11} {'':<20} reason: {row.reason}")

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "run_dir",
                    "policy",
                    "label",
                    "recovery_sec",
                    "final_goodput_ratio",
                    "final_success_pct",
                    "prefault_p95_ms",
                    "cooldown_p95_ms",
                    "reason",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "run_dir": str(row.summary.parent),
                        "policy": row.policy,
                        "label": row.label,
                        "recovery_sec": "" if row.recovery_sec is None else row.recovery_sec,
                        "final_goodput_ratio": row.final_goodput_ratio,
                        "final_success_pct": row.final_success_pct,
                        "prefault_p95_ms": row.prefault_p95_ms,
                        "cooldown_p95_ms": row.cooldown_p95_ms,
                        "reason": row.reason,
                    }
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
