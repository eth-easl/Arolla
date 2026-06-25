#!/usr/bin/env python3
"""Sustained-recovery metric.

The legacy ``analyze.recovery_time_sec`` marks recovery at the *first*
single-second crossing of an absolute 95% success rate, so a run that briefly
touches the bar and then collapses (metastable) still gets an optimistic finite
recovery time. This module adds a robust, additive metric:

  sustained_recovery_sec = the earliest t >= fault_end such that, for every
  1-second bucket in [t, t + W], BOTH
      success rate >= sr_frac   * prefault_SR        (relative, default 95%)
      goodput      >= goodput_frac * prefault_goodput (relative, default 90%)
  hold continuously. None if no such window exists in the captured span.

It is framework-agnostic: callers pass plain ``{bin: value}`` maps (bins are
integer seconds relative to some t_ref), so both ``analyze.py`` (pandas Series
via ``.to_dict()``) and the HTML report builder can share one implementation.
"""

from __future__ import annotations

import math
from typing import Mapping, Optional

DEFAULT_WINDOW_SEC = 30
DEFAULT_SR_FRAC = 0.95
DEFAULT_GOODPUT_FRAC = 0.90


def _is_ok(value) -> bool:
    return value is not None and not (isinstance(value, float) and math.isnan(value))


def sustained_recovery_sec(
    sr_by_bin: Mapping[int, float],
    goodput_by_bin: Mapping[int, float],
    *,
    prefault_sr_pct: float,
    prefault_goodput: float,
    fault_end_bin: int,
    search_end_bin: int,
    window_sec: int = DEFAULT_WINDOW_SEC,
    sr_frac: float = DEFAULT_SR_FRAC,
    goodput_frac: float = DEFAULT_GOODPUT_FRAC,
    sr_tol_frac: Optional[float] = None,
    goodput_tol_frac: Optional[float] = None,
    goodput_mean_frac: Optional[float] = None,
    min_delay_sec: int = 0,
) -> Optional[float]:
    """Earliest sustained-recovery offset (seconds after fault end), or None.

    ``sr_by_bin`` is success rate in percent keyed by integer second-bin;
    ``goodput_by_bin`` is successful req/s keyed by the same bins. ``search_end_bin``
    is the exclusive upper bound of the region to search (e.g. recovery end, or
    the next spike's start). The window covers ``window_sec`` consecutive bins
    ``[t, t + window_sec - 1]`` and must fit entirely below ``search_end_bin``.

    When ``sr_tol_frac`` / ``goodput_tol_frac`` are set, each metric must stay
    within ±that fraction of its prefault mean (symmetric band). Otherwise the
    legacy lower-bound checks (``sr_frac``, ``goodput_frac``) apply per bin.

    When ``goodput_mean_frac`` is set, goodput is checked as the mean over the
    window (caller should pass a smoothed series, e.g. 5 s rolling average)
    rather than a per-bin floor.

    ``min_delay_sec`` skips windows that start before ``fault_end_bin +
    min_delay_sec``; the returned offset is still ``t - fault_end_bin``
    (absolute seconds after fault end, not after the delay).
    """
    if sr_tol_frac is not None:
        sr_lo = (1.0 - sr_tol_frac) * prefault_sr_pct
        sr_hi = (1.0 + sr_tol_frac) * prefault_sr_pct
    else:
        sr_lo = sr_frac * prefault_sr_pct
        sr_hi = None
    use_gp_mean = goodput_mean_frac is not None
    if goodput_tol_frac is not None:
        gp_lo = (1.0 - goodput_tol_frac) * prefault_goodput
        gp_hi = (1.0 + goodput_tol_frac) * prefault_goodput
    elif not use_gp_mean:
        gp_lo = goodput_frac * prefault_goodput
        gp_hi = None
    else:
        gp_lo = gp_hi = None
    gp_mean_target = (
        goodput_mean_frac * prefault_goodput if use_gp_mean else None
    )

    def _sr_ok(b: int) -> bool:
        sr = sr_by_bin.get(b)
        if not _is_ok(sr):
            return False
        if sr < sr_lo or (sr_hi is not None and sr > sr_hi):
            return False
        return True

    def _gp_bin_ok(b: int) -> bool:
        gp = goodput_by_bin.get(b)
        if not _is_ok(gp):
            return False
        if gp_lo is not None and gp < gp_lo:
            return False
        if gp_hi is not None and gp > gp_hi:
            return False
        return True

    def _window_ok(t: int) -> bool:
        bins = range(t, t + window_sec)
        if not all(_sr_ok(b) for b in bins):
            return False
        if use_gp_mean:
            vals = [goodput_by_bin[b] for b in bins]
            if not all(_is_ok(v) for v in vals):
                return False
            return sum(vals) / len(vals) >= gp_mean_target
        return all(_gp_bin_ok(b) for b in bins)

    search_start = fault_end_bin + min_delay_sec
    last_start = search_end_bin - window_sec
    for t in range(search_start, last_start + 1):
        if _window_ok(t):
            return float(t - fault_end_bin)
    return None


def _bin(timestamp, t_ref: float) -> int:
    return int(round(float(timestamp) - t_ref))


def _spike_windows(timeline: Mapping, t_ref: float):
    """Yield (fault_end_bin, search_end_bin) per spike, in order.

    For spike i < N the search window runs until spike i+1 starts; for the last
    spike it runs until recovery end. Falls back to the single fault window when
    ``num_spikes`` <= 1.
    """
    num_spikes = int(timeline.get("num_spikes", 1) or 1)
    recovery_end_bin = _bin(timeline.get("t_recovery_end", 0), t_ref)

    if num_spikes <= 1:
        end_ts = timeline.get("t_fault_actual_end") or timeline.get("t_fault_end")
        yield _bin(end_ts, t_ref), recovery_end_bin
        return

    for i in range(1, num_spikes + 1):
        end_ts = (
            timeline.get(f"t_spike_{i}_actual_end")
            or timeline.get(f"t_spike_{i}_end")
        )
        end_bin = _bin(end_ts, t_ref)
        if i < num_spikes:
            nxt = (
                timeline.get(f"t_spike_{i + 1}_actual_start")
                or timeline.get(f"t_spike_{i + 1}_start")
            )
            search_end_bin = _bin(nxt, t_ref)
        else:
            search_end_bin = recovery_end_bin
        yield end_bin, search_end_bin


def _label_for(per_spike, num_spikes: int) -> str:
    # Count leading consecutive spikes that recovered.
    leading = 0
    for v in per_spike:
        if v is None:
            break
        leading += 1
    if num_spikes <= 1:
        return "recovered" if leading >= 1 else "never"
    if leading >= num_spikes:
        return "recovered_twice" if num_spikes == 2 else f"recovered_all_{num_spikes}"
    if leading >= 1:
        return "recovered_once"
    return "never"


def per_spike_recovery(
    timeline: Mapping,
    sr_by_bin: Mapping[int, float],
    goodput_by_bin: Mapping[int, float],
    *,
    prefault_sr_pct: float,
    prefault_goodput: float,
    t_ref: float,
    window_sec: int = DEFAULT_WINDOW_SEC,
    sr_frac: float = DEFAULT_SR_FRAC,
    goodput_frac: float = DEFAULT_GOODPUT_FRAC,
    sr_tol_frac: Optional[float] = None,
    goodput_tol_frac: Optional[float] = None,
    goodput_mean_frac: Optional[float] = None,
    min_delay_sec: int = 0,
) -> dict:
    """Sustained recovery computed independently after each fault spike.

    Returns ``{"num_spikes": N, "per_spike_sec": [...], "label": ...}`` where
    each entry is the sustained-recovery offset after that spike (or None), and
    the label summarises how many consecutive spikes the system recovered from
    (``recovered_twice`` / ``recovered_once`` / ``never`` for the 2-spike case;
    ``recovered`` / ``never`` for single-spike runs).
    """
    num_spikes = int(timeline.get("num_spikes", 1) or 1)
    per_spike = []
    for fault_end_bin, search_end_bin in _spike_windows(timeline, t_ref):
        per_spike.append(
            sustained_recovery_sec(
                sr_by_bin, goodput_by_bin,
                prefault_sr_pct=prefault_sr_pct,
                prefault_goodput=prefault_goodput,
                fault_end_bin=fault_end_bin,
                search_end_bin=search_end_bin,
                window_sec=window_sec,
                sr_frac=sr_frac,
                goodput_frac=goodput_frac,
                sr_tol_frac=sr_tol_frac,
                goodput_tol_frac=goodput_tol_frac,
                goodput_mean_frac=goodput_mean_frac,
                min_delay_sec=min_delay_sec,
            )
        )
    return {
        "num_spikes": num_spikes,
        "per_spike_sec": per_spike,
        "label": _label_for(per_spike, num_spikes),
    }
