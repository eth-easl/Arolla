#!/usr/bin/env python3
"""Cross-bench decision-drift sanity check.

Replays each tick's stored ``observation_fields`` vector through the model
loaded from the run's config (or, equivalently, looks up the recorded
``pct_idx``/``mrc_idx`` action) and reports how many ticks have a different
selected action between two bench output roots. Drift = 0 is the bar that
purely-transport-changing bench rounds must clear; an obs-source-changing
bench round may have minor drift but it must be reported.

The "spot-check ≥ 15 random ticks" pattern is the default mode here:
random sampling from the union of scenarios. Set ``--all-ticks`` to
compare every tick on every scenario when you want a hard guarantee
instead of a sample.

Two ticks "match" if and only if they share the same scenario, the same
``observation_fields`` vector (within ``--feature-eps``), and resolve to the
same ``(pct_idx, mrc_idx)`` action. We index by scenario + observation
vector rather than by tick number because two bench runs do not align in
wall-clock time (a faster observation transport produces slightly more
ticks per scenario).

Usage::

    prototype/experiments/bench_decision_diff.py \\
        outputs/prototype/rb-rl-v3-bench/phase1 \\
        outputs/prototype/rb-rl-v3-bench/phase2 \\
        --out outputs/prototype/rb-rl-v3-bench/phase2/drift.md
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

# Feature order has to match the controller's `OBSERVATION_FIELDS`.
# Duplicated here so this script can run without importing rl_controller
# (which pulls in torch + SB3). If a future change adds a feature, the
# diff harness will silently truncate to whichever side has the longer
# vector — the bench tooling is best-effort, not the source of truth.
OBSERVATION_FIELDS_DEFAULT = (
    "success_rate_agg",
    "min_client_success",
    "retry_ratio",
    "window_load_amplification",
    "window_retry_efficiency",
    "retry_fairness_gap",
    "p95_latency_pressure",
    "queue_utilization",
    "server_fail_rate",
    "deadline_rate",
    "delta_success_agg",
    "delta_window_load_amplification",
    "budget_utilization",
    "retry_pressure_vs_limit",
    "current_percent_norm",
    "current_min_retry_concurrency_norm",
    "previous_percent_norm",
    "previous_min_retry_concurrency_norm",
)


def find_observations(root: Path) -> dict[str, Path]:
    """Scenario label → rl-observations.jsonl, matching bench_summarize.py."""
    out: dict[str, Path] = {}
    for path in sorted(root.rglob("rl-observations.jsonl")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        parts = rel.parts
        if len(parts) >= 3:
            label = f"{parts[0]}/{parts[1]}"
        else:
            label = "/".join(parts)
        out[label] = path
    return out


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def obs_key(obs: list[float], eps: float) -> tuple:
    """Quantise a float vector so two near-identical observations hash the
    same way. The default eps (1e-6) treats the original feature values as
    exact; bump it if you want to tolerate floating-point drift between a
    Mac and a Linux pod (rare, but nonzero with torch ops in different
    BLAS backends)."""
    if eps <= 0:
        return tuple(obs)
    scale = 1.0 / eps
    return tuple(round(x * scale) / scale for x in obs)


def index_by_obs(rows: list[dict[str, Any]], eps: float) -> dict[tuple, dict[str, Any]]:
    """Map quantised obs → first row that produced it. We keep the first
    occurrence so the comparison is deterministic when an obs vector
    repeats (which happens during long no-change periods)."""
    out: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        obs = r.get("observation")
        if not isinstance(obs, list):
            continue
        key = obs_key(obs, eps)
        out.setdefault(key, r)
    return out


def action_pair(row: dict[str, Any]) -> tuple[int | None, int | None]:
    return (row.get("pct_idx"), row.get("mrc_idx"))


def diff_scenarios(
    root_a: dict[str, Path],
    root_b: dict[str, Path],
    sample_size: int | None,
    feature_eps: float,
    rng: random.Random,
) -> dict[str, Any]:
    matched_scenarios = sorted(set(root_a) & set(root_b))
    only_a = sorted(set(root_a) - set(root_b))
    only_b = sorted(set(root_b) - set(root_a))

    per_scenario: list[dict[str, Any]] = []
    total_compared = 0
    total_drift = 0
    for label in matched_scenarios:
        rows_a = load_jsonl(root_a[label])
        rows_b = load_jsonl(root_b[label])
        index_b = index_by_obs(rows_b, feature_eps)

        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for ra in rows_a:
            obs = ra.get("observation")
            if not isinstance(obs, list):
                continue
            rb = index_b.get(obs_key(obs, feature_eps))
            if rb is not None:
                matched.append((ra, rb))

        if sample_size and len(matched) > sample_size:
            sample = rng.sample(matched, sample_size)
        else:
            sample = matched

        scenario_drift = 0
        examples: list[dict[str, Any]] = []
        for ra, rb in sample:
            if action_pair(ra) != action_pair(rb):
                scenario_drift += 1
                if len(examples) < 5:
                    examples.append({
                        "action_a": action_pair(ra),
                        "action_b": action_pair(rb),
                        "observation_a_ts": ra.get("timestamp"),
                        "observation_b_ts": rb.get("timestamp"),
                    })

        total_compared += len(sample)
        total_drift += scenario_drift
        per_scenario.append({
            "scenario": label,
            "ticks_a": len(rows_a),
            "ticks_b": len(rows_b),
            "matched": len(matched),
            "compared": len(sample),
            "drift": scenario_drift,
            "examples": examples,
        })

    return {
        "roots": {
            "a_only_scenarios": only_a,
            "b_only_scenarios": only_b,
            "matched_scenarios": matched_scenarios,
        },
        "per_scenario": per_scenario,
        "total_compared": total_compared,
        "total_drift": total_drift,
    }


def diff_features(
    root_a: dict[str, Path],
    root_b: dict[str, Path],
    sample_size: int | None,
    feature_names: tuple[str, ...],
    rng: random.Random,
) -> dict[str, Any]:
    """Per-feature drift across matched (scenario, tick_index) pairs.

    Matching by tick_index — rather than the obs-vector index used for
    action drift — is deliberately tolerant: two runs of the same scenario
    do not share observation vectors (the loader sees different live
    traffic), but they do share scheduling, so tick i of run A and tick i
    of run B both observe roughly the same wall-clock slice of the
    scenario timeline.

    For every matched pair this records the absolute feature delta and
    the absolute feature value (averaged across the two sides). The
    reported metric is ``mean(|Δ_i|) / mean(|x_i|)`` — the standard
    "relative mean delta" used as the cross-bench acceptance bar. We use
    mean rather than per-tick relative because counter features are
    often 0 on a given tick, which would blow up a per-tick ratio.
    """
    matched_scenarios = sorted(set(root_a) & set(root_b))
    per_scenario: list[dict[str, Any]] = []
    feature_totals: dict[str, dict[str, float]] = {
        name: {"abs_delta_sum": 0.0, "abs_value_sum": 0.0, "n": 0}
        for name in feature_names
    }

    for label in matched_scenarios:
        rows_a = load_jsonl(root_a[label])
        rows_b = load_jsonl(root_b[label])

        # Index by tick_index for matching. If multiple ticks share an
        # index (shouldn't happen, but guard anyway) keep the first.
        ix_a = {r.get("tick_index"): r for r in rows_a if r.get("tick_index") is not None}
        ix_b = {r.get("tick_index"): r for r in rows_b if r.get("tick_index") is not None}
        common = sorted(set(ix_a) & set(ix_b))
        matched_pairs = [(ix_a[i], ix_b[i]) for i in common]

        if sample_size and len(matched_pairs) > sample_size:
            matched_pairs = rng.sample(matched_pairs, sample_size)

        per_feature_local: dict[str, dict[str, float]] = {
            name: {"abs_delta_sum": 0.0, "abs_value_sum": 0.0, "n": 0}
            for name in feature_names
        }

        for ra, rb in matched_pairs:
            obs_a = ra.get("observation")
            obs_b = rb.get("observation")
            if not (isinstance(obs_a, list) and isinstance(obs_b, list)):
                continue
            # Length mismatch: truncate to the shorter vector and let the
            # operator notice via the per-feature `n` column.
            common_len = min(len(obs_a), len(obs_b), len(feature_names))
            for i in range(common_len):
                name = feature_names[i]
                va = float(obs_a[i])
                vb = float(obs_b[i])
                avg = 0.5 * (abs(va) + abs(vb))
                per_feature_local[name]["abs_delta_sum"] += abs(va - vb)
                per_feature_local[name]["abs_value_sum"] += avg
                per_feature_local[name]["n"] += 1
                feature_totals[name]["abs_delta_sum"] += abs(va - vb)
                feature_totals[name]["abs_value_sum"] += avg
                feature_totals[name]["n"] += 1

        per_scenario.append({
            "scenario": label,
            "matched": len(matched_pairs),
            "per_feature": {
                name: {
                    "n": stats["n"],
                    "abs_delta_mean": (
                        stats["abs_delta_sum"] / stats["n"] if stats["n"] else 0.0
                    ),
                    "abs_value_mean": (
                        stats["abs_value_sum"] / stats["n"] if stats["n"] else 0.0
                    ),
                    "rel_drift": (
                        stats["abs_delta_sum"] / stats["abs_value_sum"]
                        if stats["abs_value_sum"] > 0 else 0.0
                    ),
                }
                for name, stats in per_feature_local.items()
            },
        })

    aggregate = {
        name: {
            "n": stats["n"],
            "abs_delta_mean": (
                stats["abs_delta_sum"] / stats["n"] if stats["n"] else 0.0
            ),
            "abs_value_mean": (
                stats["abs_value_sum"] / stats["n"] if stats["n"] else 0.0
            ),
            "rel_drift": (
                stats["abs_delta_sum"] / stats["abs_value_sum"]
                if stats["abs_value_sum"] > 0 else 0.0
            ),
        }
        for name, stats in feature_totals.items()
    }

    return {
        "matched_scenarios": matched_scenarios,
        "feature_names": list(feature_names),
        "aggregate": aggregate,
        "per_scenario": per_scenario,
    }


def render_feature_markdown(
    report: dict[str, Any], root_a: Path, root_b: Path,
) -> str:
    lines: list[str] = []
    lines.append(
        f"# Per-feature observation drift — `{root_a.name}` → `{root_b.name}`"
    )
    lines.append("")
    lines.append(
        "Matched by `(scenario, tick_index)`. Reported metric per feature: "
        "`rel_drift = mean(|Δ|) / mean(|x|)` (averaged across the two roots). "
        "Acceptance bar for pure-transport refactors: every feature `≤ 1 %`."
    )
    lines.append("")

    lines.append("## Aggregate across all matched ticks")
    lines.append("")
    lines.append("| feature | n | mean(|Δ|) | mean(|x|) | rel_drift |")
    lines.append("|---|---:|---:|---:|---:|")
    for name in report["feature_names"]:
        s = report["aggregate"][name]
        lines.append(
            f"| {name} | {s['n']} | {s['abs_delta_mean']:.6f} | "
            f"{s['abs_value_mean']:.6f} | {s['rel_drift'] * 100:.3f} % |"
        )
    lines.append("")

    if report["per_scenario"]:
        lines.append("## Per-scenario rel_drift (%)")
        lines.append("")
        header = "| scenario | matched | " + " | ".join(report["feature_names"]) + " |"
        sep = "|---|---:|" + "|".join(["---:"] * len(report["feature_names"])) + "|"
        lines.append(header)
        lines.append(sep)
        for row in report["per_scenario"]:
            cells = [
                f"{row['per_feature'][name]['rel_drift'] * 100:.3f}"
                for name in report["feature_names"]
            ]
            lines.append(
                f"| {row['scenario']} | {row['matched']} | " + " | ".join(cells) + " |"
            )
        lines.append("")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any], root_a: Path, root_b: Path) -> str:
    lines: list[str] = []
    lines.append(f"# Decision drift — `{root_a.name}` → `{root_b.name}`")
    lines.append("")
    lines.append(
        f"Compared {report['total_compared']} matched ticks; "
        f"**{report['total_drift']}** had different actions."
    )
    if report["roots"]["a_only_scenarios"]:
        lines.append("")
        lines.append("Scenarios present only in A: "
                     + ", ".join(report["roots"]["a_only_scenarios"]))
    if report["roots"]["b_only_scenarios"]:
        lines.append("")
        lines.append("Scenarios present only in B: "
                     + ", ".join(report["roots"]["b_only_scenarios"]))
    lines.append("")
    lines.append("| scenario | ticks A | ticks B | matched | compared | drift |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in report["per_scenario"]:
        lines.append(
            f"| {row['scenario']} | {row['ticks_a']} | {row['ticks_b']} | "
            f"{row['matched']} | {row['compared']} | {row['drift']} |"
        )

    drift_examples = [
        (r["scenario"], ex)
        for r in report["per_scenario"]
        for ex in r["examples"]
    ]
    if drift_examples:
        lines.append("")
        lines.append("## Sample drifts (first 5 per scenario)")
        lines.append("")
        for scenario, ex in drift_examples:
            lines.append(
                f"- **{scenario}** A={ex['action_a']} "
                f"B={ex['action_b']}  "
                f"(ts_a={ex['observation_a_ts']}, ts_b={ex['observation_b_ts']})"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("root_a", type=Path, help="earlier bench root")
    parser.add_argument("root_b", type=Path, help="later bench root")
    parser.add_argument(
        "--sample-size", type=int, default=15,
        help="Per-scenario sample size (default 15).",
    )
    parser.add_argument(
        "--all-ticks", action="store_true",
        help="Compare every matched tick (overrides --sample-size).",
    )
    parser.add_argument(
        "--feature-eps", type=float, default=1e-6,
        help="Quantisation when matching observation vectors across roots.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write markdown here (default: <root_b>/drift.md). '-' for stdout.",
    )
    parser.add_argument(
        "--per-feature", action="store_true",
        help=(
            "Report per-feature observation drift instead of action drift. "
            "Matches by (scenario, tick_index) and emits "
            "mean(|Δ_i|) / mean(|x_i|) for each of the 18 features. Used "
            "to validate that a transport refactor does not move the "
            "observation distribution."
        ),
    )
    args = parser.parse_args()

    root_a: Path = args.root_a.resolve()
    root_b: Path = args.root_b.resolve()
    if not root_a.is_dir() or not root_b.is_dir():
        print("error: root_a and root_b must both be directories", file=sys.stderr)
        return 2

    obs_a = find_observations(root_a)
    obs_b = find_observations(root_b)
    if not obs_a or not obs_b:
        print("warning: missing rl-observations.jsonl on one or both sides", file=sys.stderr)

    sample_size = None if args.all_ticks else args.sample_size

    if args.per_feature:
        feat_report = diff_features(
            obs_a, obs_b,
            sample_size=sample_size,
            feature_names=OBSERVATION_FIELDS_DEFAULT,
            rng=random.Random(args.seed),
        )
        md = render_feature_markdown(feat_report, root_a, root_b)
        worst_name, worst_value = max(
            (
                (name, stats["rel_drift"])
                for name, stats in feat_report["aggregate"].items()
            ),
            key=lambda kv: kv[1],
            default=("(none)", 0.0),
        )
    else:
        report = diff_scenarios(
            obs_a, obs_b,
            sample_size=sample_size,
            feature_eps=args.feature_eps,
            rng=random.Random(args.seed),
        )
        md = render_markdown(report, root_a, root_b)

    if args.out is None:
        out_path = root_b / "drift.md"
    elif str(args.out) == "-":
        sys.stdout.write(md)
        return 0
    else:
        out_path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    if args.per_feature:
        print(
            f"wrote {out_path}  worst_feature={worst_name} "
            f"rel_drift={worst_value * 100:.3f}%"
        )
    else:
        print(
            f"wrote {out_path}  drift={report['total_drift']}/{report['total_compared']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
