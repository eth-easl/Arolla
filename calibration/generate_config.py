#!/usr/bin/env python3
"""
Generate a simulator YAML config from fitted parameters and hardcoded topology.

Reads fitted_params.json (output of fit.py) and produces a ready-to-run
simulator YAML that models all 11 online-boutique services with their real
latency distributions and dependency graph.

Usage:
    python3 generate_config.py fitted_params.json [--out online_boutique.yaml]
    python3 generate_config.py fitted_params.json --rps 15.0 --duration 300
"""

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml  # PyYAML (pip install pyyaml)

# Add calibration dir to path for topology import
sys.path.insert(0, str(Path(__file__).parent))
from topology import SERVICE_ORDER, DEPENDENCIES, CALL_PATTERNS, SERVICE_PORTS

# Add simulator src to path for validation
_SIMULATOR_SRC = Path(__file__).parent.parent / "simulator" / "src"
sys.path.insert(0, str(_SIMULATOR_SRC))


# ── Dependency-latency correction ─────────────────────────────────────────────

def correct_own_compute_times(fitted_services: dict) -> dict[str, float]:
    """Return corrected own-compute median_ms for every service.

    Problem: Envoy measures total request time (own_compute + all dep calls).
    The simulator uses median_ms as own_compute only, then adds dep call times
    on top.  Without correction the simulator double-counts dependency latency
    for every aggregator service.

    Fix: subtract the dep contribution from the observed total, processing
    services in leaf-first topological order so each service's full simulator
    time is known before its parents are corrected.

        sequential: dep_contribution = sum(dep_simulator_totals)
        parallel:   dep_contribution = max(dep_simulator_totals)

    Returns {svc_name: corrected_median_ms} for all services.
    """
    sim_totals: dict[str, float] = {}   # expected simulator end-to-end time
    corrected: dict[str, float] = {}

    for svc_name in SERVICE_ORDER:      # leaves first
        params = fitted_services.get(svc_name, {})
        observed = params.get("median_ms", 1.0)
        deps = DEPENDENCIES.get(svc_name, [])

        if not deps:
            corrected[svc_name] = observed
            sim_totals[svc_name] = observed
            continue

        pattern = CALL_PATTERNS.get(svc_name, "sequential")
        dep_times = [sim_totals.get(d, 1.0) for d, _ in deps]
        dep_contribution = max(dep_times) if pattern == "parallel" else sum(dep_times)

        own = max(0.5, observed - dep_contribution)
        corrected[svc_name] = own
        sim_totals[svc_name] = own + dep_contribution

        if abs(own - observed) > 0.5:
            print(
                f"    [correction] {svc_name}: observed={observed:.1f}ms"
                f"  dep_contribution={dep_contribution:.1f}ms ({pattern})"
                f"  own_compute={own:.1f}ms"
            )

    return corrected


# ── Config builder ─────────────────────────────────────────────────────────────

def build_config(fitted: dict, rps_override: float | None = None,
                 duration_s: int = 120) -> dict:
    """Build the full experiment config dict from fitted parameters.

    Services are emitted in SERVICE_ORDER (leaves first) so the simulator's
    topological-sort loader can resolve dependency references without issues.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Correct median_ms: simulator adds dep latency on top of median_ms, but
    # Envoy measures total time (own compute + deps).  Subtract dep contribution
    # so the simulator produces the same end-to-end time as observed.
    corrected_medians = correct_own_compute_times(fitted["services"])

    services = []

    for svc_name in SERVICE_ORDER:
        params = fitted["services"].get(svc_name)
        if params is None:
            print(f"  [WARN] {svc_name} missing from fitted_params — using defaults")
            params = {"median_ms": 10.0, "lognorm_sigma": 0.5, "workers": 8}

        own_median = corrected_medians.get(svc_name, params["median_ms"])

        svc_block: dict = {
            "name": svc_name,
            "latency": {
                "median_ms": round(own_median, 3),
                "lognorm_sigma": params["lognorm_sigma"],
            },
            "workers": params["workers"],
            # queue_capacity: 4× workers gives realistic shedding headroom
            "queue_capacity": params["workers"] * 4,
        }

        deps = DEPENDENCIES.get(svc_name, [])
        if deps:
            svc_block["dependencies"] = [
                {"service": dep_name, "optional": optional}
                for dep_name, optional in deps
            ]
            svc_block["dependency_call_pattern"] = CALL_PATTERNS.get(
                svc_name, "sequential"
            )

        services.append(svc_block)

    # Entry-point RPS: use override, then fitted frontend rps, then fallback
    frontend_params = fitted["services"].get("frontend", {})
    rps = rps_override or (frontend_params.get("rps") or 0.0)
    if rps < 1.0:
        print(f"  [WARN] frontend RPS={rps:.2f} < 1.0, defaulting to 10.0")
        rps = 10.0

    config = {
        "name": f"online_boutique_calibrated_{today}",
        "seed": 42,
        "services": services,
        "clients": [
            {
                "name": "user",
                "target_service": "frontend",
                "workload": {
                    "base_rps": round(rps, 1),
                    "duration_s": duration_s,
                },
            }
        ],
        "output_csv": "output.csv",
        # 10s buckets give ~28 frontend requests per bucket → per-bucket P99 ≈ true P97.
        # At 1s, each bucket has ~3 requests so "P99" = max of 3 ≈ true P75.
        "granularity_s": 10.0,
    }

    return config


# ── YAML validation via simulator ConfigLoader ────────────────────────────────

def validate_yaml(config_dict: dict) -> bool:
    """Run the config through the simulator's Pydantic schema validation.

    Returns True if valid. Prints the error and returns False on failure.
    Gracefully skips if the simulator package cannot be imported.
    """
    try:
        from simulator.config.loader import ConfigLoader
    except ImportError:
        print("  [WARN] simulator package not importable — skipping validation")
        return True

    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, dir='/tmp'
    ) as tmp:
        yaml.dump(config_dict, tmp, default_flow_style=False, sort_keys=False)
        tmp_path = tmp.name

    try:
        ConfigLoader.load_from_file(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)
        return True
    except Exception as exc:
        Path(tmp_path).unlink(missing_ok=True)
        print(f"  [ERROR] YAML validation failed: {exc}", file=sys.stderr)
        return False


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate simulator YAML from fitted parameters"
    )
    parser.add_argument(
        "fitted_params",
        help="Path to fitted_params.json (output of fit.py)"
    )
    parser.add_argument(
        "--out", default="configs/online_boutique.yaml",
        help="Output YAML file (default: configs/online_boutique.yaml)"
    )
    parser.add_argument(
        "--rps", type=float, default=None,
        help="Override frontend base_rps (default: use value from fitted_params)"
    )
    parser.add_argument(
        "--duration", type=int, default=120,
        help="Simulation duration in seconds (default: 120)"
    )
    args = parser.parse_args()

    fitted_path = Path(args.fitted_params)
    if not fitted_path.exists():
        print(f"Error: not found: {fitted_path}", file=sys.stderr)
        return 1

    fitted = json.loads(fitted_path.read_text())
    collected_at = fitted.get("collected_at", "unknown")

    n_services = len(fitted.get("services", {}))
    print(f"  Building config from {n_services} fitted services...")

    config = build_config(fitted, rps_override=args.rps, duration_s=args.duration)

    print("  Validating YAML against simulator schema...")
    if not validate_yaml(config):
        print("Aborting: generated YAML is invalid.", file=sys.stderr)
        return 1
    print("  Validation passed.")

    # Serialize: prepend header comment then YAML body
    header = "\n".join([
        "# Generated by calibration/generate_config.py",
        f"# Collected: {collected_at}",
        "# Regenerate: cd calibration && ./pipeline.sh",
        "#",
        "",
    ])
    yaml_body = yaml.dump(
        config, default_flow_style=False, sort_keys=False, allow_unicode=True
    )

    out_path = Path(args.out)
    out_path.write_text(header + yaml_body)

    frontend_rps = config["clients"][0]["workload"]["base_rps"]
    duration = config["clients"][0]["workload"]["duration_s"]
    n_svcs = len(config["services"])
    print(f"  Wrote {out_path}")
    print(f"  {n_svcs} services  |  frontend @ {frontend_rps} RPS for {duration}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
