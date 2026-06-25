"""Tests for the sustained-recovery metric (TDD red-first).

The metric: earliest time after fault-end where smoothed success rate stays
>= 0.95 x prefault_SR AND goodput stays >= 0.90 x prefault_goodput for a
continuous window of W seconds. None if no such window exists.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sustained_recovery import sustained_recovery_sec, per_spike_recovery  # noqa: E402


def _const(start_bin, end_bin, value):
    """Dict {bin: value} for bins in [start_bin, end_bin)."""
    return {b: value for b in range(start_bin, end_bin)}


def _merge(*dicts):
    out = {}
    for d in dicts:
        out.update(d)
    return out


# --------------------------------------------------------------------------- #
# Single-window sustained_recovery_sec
# --------------------------------------------------------------------------- #

def test_clean_recovery_returns_zero():
    # Healthy from the moment the fault clears (bin 80) onward.
    sr = _const(80, 260, 100.0)
    gp = _const(80, 260, 1000.0)
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
    )
    assert out == 0.0


def test_delayed_recovery_returns_offset():
    # Bad for 20s after fault end, then healthy.
    sr = _merge(_const(80, 100, 50.0), _const(100, 260, 100.0))
    gp = _merge(_const(80, 100, 300.0), _const(100, 260, 1000.0))
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
    )
    assert out == 20.0


def test_oscillate_then_collapse_returns_none():
    # Touches 100% briefly, then sits at 40% forever -> never sustained.
    sr = _merge(_const(80, 101, 100.0), _const(101, 260, 40.0))
    gp = _merge(_const(80, 101, 1000.0), _const(101, 260, 400.0))
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
    )
    assert out is None


def test_high_sr_low_goodput_shed_returns_none():
    # Success rate is perfect but goodput is on the floor (load shedding).
    sr = _const(80, 260, 100.0)
    gp = _const(80, 260, 100.0)  # 10% of prefault -> below 0.90 target
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
    )
    assert out is None


def test_window_does_not_fit_returns_none():
    # Healthy, but the captured region is shorter than the window.
    sr = _const(80, 100, 100.0)
    gp = _const(80, 100, 1000.0)
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=100,
        window_sec=30,
    )
    assert out is None


def test_relative_threshold_uses_prefault_baseline():
    # Prefault SR is only 80%; steady SR of 78 (>= 0.95*80 = 76) recovers,
    # which an absolute 95% bar would wrongly reject.
    sr = _const(80, 260, 78.0)
    gp = _const(80, 260, 1000.0)
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=80.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
    )
    assert out == 0.0


def test_missing_bucket_breaks_continuity():
    # A gap (no data) inside an otherwise-healthy window must break it.
    sr = _const(80, 260, 100.0)
    gp = _const(80, 260, 1000.0)
    del sr[90]  # one missing second at bin 90
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
    )
    # First viable window must start after the gap: earliest t with all of
    # [t, t+29] present is t=91 -> 11s.
    assert out == 11.0


# --------------------------------------------------------------------------- #
# Multi-spike per_spike_recovery
# --------------------------------------------------------------------------- #

def _two_spike_timeline():
    # t_ref = 0; spike1 [50,80), gap to spike2 [140,160), recovery_end 340.
    return {
        "num_spikes": 2,
        "t_warmup_end": 0,
        "t_fault_start": 50,
        "t_fault_end": 160,
        "t_recovery_end": 340,
        "t_spike_1_actual_start": 50,
        "t_spike_1_actual_end": 80,
        "t_spike_2_actual_start": 140,
        "t_spike_2_actual_end": 160,
    }


def test_multispike_recovered_twice():
    sr = _const(80, 340, 100.0)
    gp = _const(80, 340, 1000.0)
    out = per_spike_recovery(
        _two_spike_timeline(), sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        t_ref=0.0, window_sec=30,
    )
    assert out["num_spikes"] == 2
    assert out["per_spike_sec"] == [0.0, 0.0]
    assert out["label"] == "recovered_twice"


def test_multispike_recover_once_then_trap():
    # Healthy after spike 1 (bins 80..139), collapsed after spike 2.
    sr = _merge(_const(80, 140, 100.0), _const(160, 340, 45.0))
    gp = _merge(_const(80, 140, 1000.0), _const(160, 340, 300.0))
    out = per_spike_recovery(
        _two_spike_timeline(), sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        t_ref=0.0, window_sec=30,
    )
    assert out["per_spike_sec"][0] == 0.0
    assert out["per_spike_sec"][1] is None
    assert out["label"] == "recovered_once"


def test_multispike_never_recovers():
    sr = _const(80, 340, 50.0)
    gp = _const(80, 340, 300.0)
    out = per_spike_recovery(
        _two_spike_timeline(), sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        t_ref=0.0, window_sec=30,
    )
    assert out["per_spike_sec"] == [None, None]
    assert out["label"] == "never"


def test_goodput_mean_over_window():
    sr = _const(80, 260, 100.0)
    gp = _const(80, 260, 960.0)  # 96% of prefault mean over window
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
        sr_frac=0.95,
        goodput_mean_frac=0.95,
    )
    assert out == 0.0


def test_goodput_mean_rejects_low_average():
    sr = _const(80, 260, 100.0)
    gp = _const(80, 260, 900.0)  # 90% of prefault -> mean below 95% target
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=30,
        sr_frac=0.95,
        goodput_mean_frac=0.95,
    )
    assert out is None


def test_min_delay_skips_early_windows():
    # Healthy immediately at fault end, but min_delay=15 -> R=15 not 0.
    sr = _const(80, 260, 100.0)
    gp = _const(80, 260, 1000.0)
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=45,
        min_delay_sec=15,
    )
    assert out == 15.0


def test_min_delay_delayed_recovery_adds_offset():
    # Bad until bin 100, then healthy; min_delay=15 -> search from 95.
    sr = _merge(_const(80, 100, 50.0), _const(100, 260, 100.0))
    gp = _merge(_const(80, 100, 300.0), _const(100, 260, 1000.0))
    out = sustained_recovery_sec(
        sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        fault_end_bin=80, search_end_bin=260,
        window_sec=45,
        min_delay_sec=15,
    )
    assert out == 20.0


def test_single_spike_timeline_uses_recovery_window():
    tl = {
        "num_spikes": 1,
        "t_warmup_end": 0,
        "t_fault_start": 50,
        "t_fault_end": 80,
        "t_recovery_end": 260,
    }
    sr = _const(80, 260, 100.0)
    gp = _const(80, 260, 1000.0)
    out = per_spike_recovery(
        tl, sr, gp,
        prefault_sr_pct=100.0, prefault_goodput=1000.0,
        t_ref=0.0, window_sec=30,
    )
    assert out["num_spikes"] == 1
    assert out["per_spike_sec"] == [0.0]
    assert out["label"] == "recovered"
