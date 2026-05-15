#!/usr/bin/env python3
"""Cross-phase decision-drift sanity check (plan-12 §6 step 4).

Replays each tick's stored ``observation_fields`` vector through the model
loaded from the run's config (or, equivalently, looks up the recorded
``pct_idx``/``mrc_idx`` action) and reports how many ticks have a different
selected action between two phase output roots. Drift = 0 is the bar Phases
1 and 2 must clear; Phase 3 may have minor drift but it must be reported.

The "spot-check ≥ 15 random ticks" wording in the plan is implemented as the
default mode here: random sampling from the union of scenarios. Set
``--all-ticks`` to compare every tick on every scenario when you want a
hard guarantee instead of a sample.

Two ticks "match" if and only if they share the same scenario, the same
``observation_fields`` vector (within ``--feature-eps``), and resolve to the
same ``(pct_idx, mrc_idx)`` action. We index by scenario + observation
vector rather than by tick number because Phase 1 and Phase 2 ticks don't
align in wall-clock time (HTTP fetch is faster, so there are slightly more
ticks in Phase 2).

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
    phase_a: dict[str, Path],
    phase_b: dict[str, Path],
    sample_size: int | None,
    feature_eps: float,
    rng: random.Random,
) -> dict[str, Any]:
    matched_scenarios = sorted(set(phase_a) & set(phase_b))
    only_a = sorted(set(phase_a) - set(phase_b))
    only_b = sorted(set(phase_b) - set(phase_a))

    per_scenario: list[dict[str, Any]] = []
    total_compared = 0
    total_drift = 0
    for label in matched_scenarios:
        rows_a = load_jsonl(phase_a[label])
        rows_b = load_jsonl(phase_b[label])
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
                        "phase_a_action": action_pair(ra),
                        "phase_b_action": action_pair(rb),
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
        "phases": {
            "a_only_scenarios": only_a,
            "b_only_scenarios": only_b,
            "matched_scenarios": matched_scenarios,
        },
        "per_scenario": per_scenario,
        "total_compared": total_compared,
        "total_drift": total_drift,
    }


def render_markdown(report: dict[str, Any], phase_a: Path, phase_b: Path) -> str:
    lines: list[str] = []
    lines.append(f"# Decision drift — `{phase_a.name}` → `{phase_b.name}`")
    lines.append("")
    lines.append(
        f"Compared {report['total_compared']} matched ticks; "
        f"**{report['total_drift']}** had different actions."
    )
    if report["phases"]["a_only_scenarios"]:
        lines.append("")
        lines.append("Scenarios present only in A: "
                     + ", ".join(report["phases"]["a_only_scenarios"]))
    if report["phases"]["b_only_scenarios"]:
        lines.append("")
        lines.append("Scenarios present only in B: "
                     + ", ".join(report["phases"]["b_only_scenarios"]))
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
                f"- **{scenario}** A={ex['phase_a_action']} "
                f"B={ex['phase_b_action']}  "
                f"(ts_a={ex['observation_a_ts']}, ts_b={ex['observation_b_ts']})"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("phase_a", type=Path, help="earlier phase root")
    parser.add_argument("phase_b", type=Path, help="later phase root")
    parser.add_argument(
        "--sample-size", type=int, default=15,
        help="Per-scenario sample size (default 15, matching plan-12 §1).",
    )
    parser.add_argument(
        "--all-ticks", action="store_true",
        help="Compare every matched tick (overrides --sample-size).",
    )
    parser.add_argument(
        "--feature-eps", type=float, default=1e-6,
        help="Quantisation when matching observation vectors across phases.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write markdown here (default: <phase_b>/drift.md). '-' for stdout.",
    )
    args = parser.parse_args()

    phase_a: Path = args.phase_a.resolve()
    phase_b: Path = args.phase_b.resolve()
    if not phase_a.is_dir() or not phase_b.is_dir():
        print("error: phase_a and phase_b must both be directories", file=sys.stderr)
        return 2

    obs_a = find_observations(phase_a)
    obs_b = find_observations(phase_b)
    if not obs_a or not obs_b:
        print("warning: missing rl-observations.jsonl on one or both sides", file=sys.stderr)

    sample_size = None if args.all_ticks else args.sample_size
    report = diff_scenarios(
        obs_a, obs_b,
        sample_size=sample_size,
        feature_eps=args.feature_eps,
        rng=random.Random(args.seed),
    )
    md = render_markdown(report, phase_a, phase_b)

    if args.out is None:
        out_path = phase_b / "drift.md"
    elif str(args.out) == "-":
        sys.stdout.write(md)
        return 0
    else:
        out_path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    print(f"wrote {out_path}  drift={report['total_drift']}/{report['total_compared']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
