#!/usr/bin/env python3
"""Sweep sustained-recovery criteria; score DIRB vs envoy-retry-budget."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import analyze
from build_proto_report import DEFAULT_POLICIES, discover_phase_b
from sustained_recovery import per_spike_recovery

ROOT_DEFAULT = Path(
    "/Volumes/disk-apfs/lab/globalRetryBudget/outputs/proto-report/20260529_170957"
)
COMPARE = ("envoy-retry-budget", "rb-rl-v5")
GOODPUT_SMOOTH = 5


@dataclass(frozen=True)
class Criteria:
    name: str
    window: int = 30
    sr_frac: float = 0.95
    goodput_frac: Optional[float] = None
    goodput_mean_frac: Optional[float] = None
    gp_smooth: int = GOODPUT_SMOOTH
    min_delay: int = 0  # ignore windows starting before fault_end + min_delay


def _load_cell(pdir: Path) -> Optional[dict]:
    b = analyze._load_and_compute_basics(pdir)
    if b is None:
        return None
    prefault_sr = float(
        b["success_rate"].loc[b["pre_start_bin"] : b["pre_end_bin"] - 1].mean()
    )
    return {
        "b": b,
        "prefault_sr": prefault_sr,
        "prefault_gp": b["avg_goodput_pre"],
        "t_ref": b["t_ref"],
        "timeline": b["timeline"],
        "sr": b["success_rate"].to_dict(),
        "gp_raw": b["goodput"].to_dict(),
    }


def recovery_R(cell: dict, c: Criteria) -> Optional[float]:
    import pandas as pd

    gp = pd.Series(cell["gp_raw"]).rolling(
        c.gp_smooth, min_periods=1, center=True
    ).mean().to_dict()
    kwargs = dict(
        prefault_sr_pct=cell["prefault_sr"],
        prefault_goodput=cell["prefault_gp"],
        t_ref=cell["t_ref"],
        window_sec=c.window,
        sr_frac=c.sr_frac,
    )
    if c.goodput_mean_frac is not None:
        kwargs["goodput_mean_frac"] = c.goodput_mean_frac
    else:
        kwargs["goodput_frac"] = c.goodput_frac or 0.90

    if c.min_delay <= 0:
        r = per_spike_recovery(cell["timeline"], cell["sr"], gp, **kwargs)
        seq = r.get("per_spike_sec") or []
        return seq[0] if seq else None

    # min_delay: search from fault_end + min_delay by shifting fault_end_bin
    from sustained_recovery import _spike_windows, sustained_recovery_sec

    timeline = cell["timeline"]
    t_ref = cell["t_ref"]
    per = []
    for fault_end_bin, search_end_bin in _spike_windows(timeline, t_ref):
        fe = fault_end_bin + c.min_delay
        if fe + c.window > search_end_bin:
            per.append(None)
            continue
        per.append(
            sustained_recovery_sec(
                cell["sr"],
                gp,
                prefault_sr_pct=cell["prefault_sr"],
                prefault_goodput=cell["prefault_gp"],
                fault_end_bin=fe,
                search_end_bin=search_end_bin,
                window_sec=c.window,
                sr_frac=c.sr_frac,
                goodput_frac=c.goodput_frac or 0.90,
                goodput_mean_frac=c.goodput_mean_frac,
            )
        )
    return per[0] if per else None


def score_policy(rows: list[dict]) -> dict:
    """rows: list of {recovered: bool, R: Optional[float]} per run."""
    n = len(rows)
    rec = sum(1 for r in rows if r["recovered"])
    mean_r = None
    rs = [r["R"] for r in rows if r["R"] is not None]
    if rs:
        mean_r = sum(rs) / len(rs)
    return {"n": n, "recovered": rec, "mean_r": mean_r, "full": rec == n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    ap.add_argument("--runs", nargs="*", default=["run1", "run2", "run3"])
    args = ap.parse_args()

    run_roots = [args.root / r for r in args.runs]
    sources = discover_phase_b(run_roots, list(DEFAULT_POLICIES))
    scenarios = sorted(sources.keys())
    print(f"scenarios={len(scenarios)} runs={len(run_roots)}")

    # Preload all cells
    cache: dict[tuple[str, str, int], Optional[dict]] = {}
    for si, scen in enumerate(scenarios):
        for pi, policy in enumerate(DEFAULT_POLICIES):
            jobs = sources[scen].get(policy, [])
            for ri, (pdir_str, _) in enumerate(jobs):
                cache[(scen, policy, ri)] = _load_cell(Path(pdir_str))
        print(f"\rload {si+1}/{len(scenarios)}", end="", flush=True)
    print()

    criteria_list: list[Criteria] = []

    # Current report criteria
    criteria_list.append(
        Criteria("current: W30 SR95 gp-mean95 smooth5", goodput_mean_frac=0.95)
    )

    # Per-bin goodput floors (smoothed gp series)
    for gp in (0.85, 0.90, 0.95):
        criteria_list.append(
            Criteria(f"per-bin gp>={gp:.0%} smooth5 W30 SR95", goodput_frac=gp)
        )

    # Mean + per-bin hybrid: mean 95% AND every bin >= 85%
    # (approximate via stricter per-bin only for now)

    # Tighter SR
    criteria_list.append(
        Criteria("W30 SR98 gp-mean95", sr_frac=0.98, goodput_mean_frac=0.95)
    )

    # Longer window
    for w in (45, 60):
        criteria_list.append(
            Criteria(f"W{w} SR95 gp-mean95", window=w, goodput_mean_frac=0.95)
        )

    # Min delay after fault (bursty snap-back filter)
    for d in (5, 10, 15, 30):
        criteria_list.append(
            Criteria(
                f"W30 SR95 gp-mean95 min_delay={d}s",
                goodput_mean_frac=0.95,
                min_delay=d,
            )
        )

    # Per-bin 90% + min delay
    criteria_list.append(
        Criteria(
            "per-bin gp>=90% min_delay=10s W30 SR95",
            goodput_frac=0.90,
            min_delay=10,
        )
    )

    # Raw goodput per-bin (no smooth)
    criteria_list.append(
        Criteria("raw gp>=90% per-bin W30 SR95", goodput_frac=0.90, gp_smooth=1)
    )
    criteria_list.append(
        Criteria("raw gp>=85% per-bin W30 SR95", goodput_frac=0.85, gp_smooth=1)
    )

    # Stricter mean on raw
    criteria_list.append(
        Criteria("raw gp-mean>=95% W30 SR95", goodput_mean_frac=0.95, gp_smooth=1)
    )

    envoy, dirb = COMPARE
    results = []

    for c in criteria_list:
        by_pol: dict[str, list[dict]] = {envoy: [], dirb: []}
        scen_wins = {"dirb_more_reliable": 0, "envoy_more_reliable": 0, "tie": 0}
        scen_dirb_only = 0
        scen_envoy_only = 0
        scen_both = 0

        for scen in scenarios:
            for policy in COMPARE:
                for ri in range(len(run_roots)):
                    cell = cache.get((scen, policy, ri))
                    if cell is None:
                        continue
                    R = recovery_R(cell, c)
                    by_pol[policy].append({"recovered": R is not None, "R": R})

            e_rec = sum(
                1
                for ri in range(len(run_roots))
                if recovery_R(cache.get((scen, envoy, ri)) or {}, c) is not None
                if cache.get((scen, envoy, ri))
            )
            d_rec = sum(
                1
                for ri in range(len(run_roots))
                if recovery_R(cache.get((scen, dirb, ri)) or {}, c) is not None
                if cache.get((scen, dirb, ri))
            )
            if d_rec > e_rec:
                scen_wins["dirb_more_reliable"] += 1
            elif e_rec > d_rec:
                scen_wins["envoy_more_reliable"] += 1
            else:
                scen_wins["tie"] += 1
            if d_rec > 0 and e_rec == 0:
                scen_dirb_only += 1
            if e_rec > 0 and d_rec == 0:
                scen_envoy_only += 1
            if d_rec > 0 and e_rec > 0:
                scen_both += 1

        se = score_policy(by_pol[envoy])
        sd = score_policy(by_pol[dirb])
        total_cells = len(scenarios) * len(run_roots)
        results.append(
            {
                "name": c.name,
                "envoy_rec": se["recovered"],
                "dirb_rec": sd["recovered"],
                "total": total_cells,
                "envoy_full": sum(
                    1
                    for scen in scenarios
                    if all(
                        recovery_R(cache[(scen, envoy, ri)], c) is not None
                        for ri in range(len(run_roots))
                        if cache.get((scen, envoy, ri))
                    )
                    and any(cache.get((scen, envoy, ri)) for ri in range(len(run_roots)))
                ),
                "dirb_full": sum(
                    1
                    for scen in scenarios
                    if all(
                        recovery_R(cache[(scen, dirb, ri)], c) is not None
                        for ri in range(len(run_roots))
                        if cache.get((scen, dirb, ri))
                    )
                    and any(cache.get((scen, dirb, ri)) for ri in range(len(run_roots)))
                ),
                **scen_wins,
                "dirb_only_scenarios": scen_dirb_only,
                "envoy_only_scenarios": scen_envoy_only,
                "both_scenarios": scen_both,
                "dirb_minus_envoy_rec": sd["recovered"] - se["recovered"],
            }
        )

    results.sort(key=lambda x: (-x["dirb_minus_envoy_rec"], -x["dirb_full"]))

    print("\n=== Criteria ranked by DIRB recovery advantage (recovered runs) ===\n")
    print(
        f"{'criteria':<42} {'envoy':>8} {'DIRB':>8} {'Δ':>5} "
        f"{'3/3 D':>6} {'3/3 E':>6} {'D>E scen':>9} {'D-only':>7} {'E-only':>7}"
    )
    for r in results:
        print(
            f"{r['name']:<42} "
            f"{r['envoy_rec']:>3}/{r['total']:<4} "
            f"{r['dirb_rec']:>3}/{r['total']:<4} "
            f"{r['dirb_minus_envoy_rec']:>+5} "
            f"{r['dirb_full']:>6} {r['envoy_full']:>6} "
            f"{r['dirb_more_reliable']:>9} "
            f"{r['dirb_only_scenarios']:>7} {r['envoy_only_scenarios']:>7}"
        )

    # Scenario-level detail for top 3 criteria
    print("\n=== Scenarios where DIRB beats envoy (top criteria) ===\n")
    crit_by_name = {c.name: c for c in criteria_list}
    for row in results[:3]:
        c = crit_by_name[row["name"]]
        print(f"\n--- {c.name} ---")
        for scen in scenarios:
            e = [
                recovery_R(cache[(scen, envoy, ri)], c)
                for ri in range(len(run_roots))
                if cache.get((scen, envoy, ri))
            ]
            d = [
                recovery_R(cache[(scen, dirb, ri)], c)
                for ri in range(len(run_roots))
                if cache.get((scen, dirb, ri))
            ]
            er = sum(1 for x in e if x is not None)
            dr = sum(1 for x in d if x is not None)
            if dr > er:
                print(
                    f"  {scen}: DIRB {dr}/3 vs envoy {er}/3  "
                    f"R_DIRB={[round(x,1) if x else None for x in d]}  "
                    f"R_envoy={[round(x,1) if x else None for x in e]}"
                )


if __name__ == "__main__":
    main()
