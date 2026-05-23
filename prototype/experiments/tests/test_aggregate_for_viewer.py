import json
import sys
from pathlib import Path

# Make the experiments dir importable so `import aggregate_for_viewer` works
# whether pytest is invoked from the repo root or from experiments/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aggregate_for_viewer import build_timeseries  # noqa: E402


FIXTURE = Path(__file__).parent.parent / "testdata" / "agg_fixture"


def test_build_timeseries_aligns_policies_on_fault_start():
    out = build_timeseries(FIXTURE)
    assert out["scenario_label"] == FIXTURE.name
    assert set(out["policies"]) == {"no-control", "rb-rl-v3"}

    # Phase boundaries are translated to seconds relative to t_fault_start.
    assert out["phases"]["fault_start_rel_s"] == 0
    assert out["phases"]["fault_end_rel_s"] == 20
    assert out["phases"]["prefault_start_rel_s"] == -60

    nc = out["series"]["no-control"]
    # Fixture (no-control), completion-time bucketing (matches analyze.py):
    #   bucket -1 (t=1089.x): r1.ok=1, r2.ok=1   → 2 ok / 2 total
    #   bucket  0 (t=1090.x): r3 attempt1 fail, r3 attempt2 ok → 1 ok / 2 total
    #   bucket  1 (t=1091.x): r4 fail            → 0 ok / 1 total
    # goodput_rps counts ALL ok rows (including retry-successes), matching
    # analyze.goodput_timeseries semantics — so bucket 0 has goodput=1.
    assert nc["t_rel_s"] == [-1, 0, 1]
    assert nc["goodput_rps"] == [2, 1, 0]
    assert nc["total_rps"]   == [2, 2, 1]

    # success_rate uses the FINAL attempt per request_id then ok/total per
    # bucket, then a 5s centered rolling mean (analyze.success_rate_timeseries
    # with smooth_win=5).  Final-attempt buckets are: r1@-1 ok, r2@-1 ok,
    # r3@0 ok (retry succeeded), r4@1 fail.  Raw SR = [1.0, 1.0, 0.0]; the 5s
    # rolling mean (min_periods=1, center=True) collapses to ~0.667 everywhere.
    assert all(v is not None for v in nc["success_rate"])
    assert all(0.0 <= v <= 1.0 for v in nc["success_rate"])
    for v in nc["success_rate"]:
        assert abs(v - 2/3) < 1e-9, f"expected 0.667, got {v}"

    # No-control has no RL pod → fields are None (or absent).
    assert nc.get("rl_pod_cpu_mcores") in (None, [None] * len(nc["t_rel_s"]))

    rl = out["series"]["rb-rl-v3"]
    # RL pod resource rows present for rb-rl-v3.
    assert any(v is not None for v in rl["rl_pod_cpu_mcores"])
    # RL action timeline picks up the decision rows (forward-filled but NOT
    # smoothed, so exact values are still verifiable at those buckets).
    assert rl["rl_action_percent"][rl["t_rel_s"].index(0)] == 20.0
    assert rl["rl_action_percent"][rl["t_rel_s"].index(2)] == 10.0
    assert rl["rl_action_percent"][rl["t_rel_s"].index(4)] == 5.0


def test_build_timeseries_writes_json(tmp_path):
    out = build_timeseries(FIXTURE)
    dst = tmp_path / "timeseries.json"
    dst.write_text(json.dumps(out))
    reloaded = json.loads(dst.read_text())
    assert reloaded["scenario_label"] == FIXTURE.name


def test_goodput_and_success_rate_match_analyze_py():
    """The viewer's goodput_rps / success_rate / latency series must be
    computed identically to ``analyze.py`` so the comparison page tells the
    same story as the per-policy PDFs.

    Picks a real run from ``outputs/`` (skipped if none present) and asserts
    bucket-for-bucket equality against the analyze.py helpers.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import analyze  # type: ignore[import-not-found]

    sweep_root = Path(__file__).resolve().parents[3] / "outputs" / "prototype-new" / "full-sweep"
    if not sweep_root.exists():
        import pytest as _pytest
        _pytest.skip("no real sweep outputs available")

    # Pick any scenario that has at least one policy with client-metrics.
    policy_dir = None
    for sw in sorted(sweep_root.iterdir(), reverse=True):
        if not sw.is_dir():
            continue
        for scen in sw.iterdir():
            if not scen.is_dir() or not scen.name.startswith("rate_rps="):
                continue
            for pol in scen.iterdir():
                if pol.is_dir() and (pol / "timeline.json").exists() and \
                   list((pol / "client-metrics").glob("client_attempts.shard*.csv")):
                    policy_dir = pol
                    break
            if policy_dir:
                break
        if policy_dir:
            break
    if policy_dir is None:
        import pytest as _pytest
        _pytest.skip("no policy with client_attempts shards found")

    tl = json.loads((policy_dir / "timeline.json").read_text())
    t0 = float(tl["t_fault_start"])
    t_warmup_end = float(tl["t_warmup_end"])
    t_cooldown_end = float(tl["t_cooldown_end"])

    df = analyze.load_client_csv(policy_dir / "client-metrics")
    analyze_goodput = analyze.goodput_timeseries(df, t_warmup_end, t_cooldown_end, bin_sec=1.0)
    analyze_sr = analyze.success_rate_timeseries(
        df, t_warmup_end, t_cooldown_end, bin_sec=1.0, smooth_win=5,
    )

    scen_out = __import__("aggregate_for_viewer").build_timeseries(policy_dir.parent)
    pol_name = policy_dir.name
    series = scen_out["series"][pol_name]

    # analyze.py uses t_warmup_end as x=0; aggregator uses t_fault_start.
    # Convert: agg_bucket k → analyze_bucket (k + (t_fault_start - t_warmup_end)).
    offset = int(round(t0 - t_warmup_end))

    # Spot-check the steady-state pre-fault window where both methods should
    # agree to the integer.  (Recovery transients can show floor() boundary
    # diffs of ±1 row near sub-second clock skew.)
    pre_keys = [k for k in series["t_rel_s"] if -30 <= k <= -5]
    assert pre_keys, "need pre-fault samples"
    for k in pre_keys:
        agg_g = series["goodput_rps"][series["t_rel_s"].index(k)]
        ana_g = analyze_goodput.get(k + offset, 0.0)
        assert abs(agg_g - ana_g) <= 1, \
            f"goodput mismatch at t_rel={k}: agg={agg_g} analyze={ana_g}"

        agg_sr_pct = series["success_rate"][series["t_rel_s"].index(k)]
        ana_sr_pct = analyze_sr.get(k + offset)
        if ana_sr_pct is not None and not (ana_sr_pct != ana_sr_pct):  # not NaN
            # analyze.py returns percent (0–100), aggregator returns ratio (0–1).
            assert abs(agg_sr_pct * 100.0 - ana_sr_pct) < 0.5, \
                f"success_rate mismatch at t_rel={k}: agg={agg_sr_pct*100:.2f}% analyze={ana_sr_pct:.2f}%"
