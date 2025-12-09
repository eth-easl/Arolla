#!/usr/bin/env python3
"""
Run experiments from YAML configuration files.

Usage:
    python bin/run_experiment.py <config.yaml> [--validate-only] [--verbose] [--output <dir>]
"""

import argparse
import sys
import os
from pathlib import Path

# Add src to path for development mode
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns


def main():
    parser = argparse.ArgumentParser(
        description="Run simulation experiment from YAML configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run experiment from YAML file
    python run_yaml_experiment.py experiments/default.yaml
    
    # Run with custom output location
    python run_yaml_experiment.py experiments/default.yaml --output results/
    
    # Validate configuration without running
    python run_yaml_experiment.py experiments/default.yaml --validate-only
        """
    )
    
    parser.add_argument(
        "config",
        type=str,
        help="Path to YAML configuration file"
    )
    
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output directory (overrides config)"
    )
    
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate configuration, don't run simulation"
    )
    
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output"
    )
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        if args.verbose:
            print(f"Loading configuration from {args.config}...")
        config = ConfigLoader.load_from_file(args.config)
        if args.verbose:
            print(f"✓ Configuration loaded: {config.name}")
            print(f"  Services: {len(config.services)}")
            print(f"  Duration: {config.workload.duration_s}s")
            print(f"  Base RPS: {config.workload.base_rps}")
    except Exception as e:
        print(f"❌ Error loading configuration: {e}", file=sys.stderr)
        return 1
    
    # Validate only
    if args.validate_only:
        print("✓ Configuration is valid")
        return 0
    
    # Build simulation
    try:
        if args.verbose:
            print("Building simulation...")
        sim, client, workload, fault_tracker = ConfigLoader.build_simulation(config)
        if args.verbose:
            print("✓ Simulation built successfully")
    except Exception as e:
        print(f"❌ Error building simulation: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    
    # Run simulation
    try:
        if args.verbose:
            print(f"Running simulation for {config.workload.duration_s}s...")
        
        # Hook workload to client
        workload.drive(sim, lambda s: client.start_request(s))
        
        # Run until workload completes
        sim.run(until=s_to_ns(workload.duration_s))
        
        # Drain outstanding requests
        sim.run()
        
        if args.verbose:
            print("✓ Simulation completed")
    except Exception as e:
        print(f"❌ Error running simulation: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    
    # Export results
    try:
        # Determine output paths
        if args.output:
            output_dir = Path(args.output)
            output_dir.mkdir(parents=True, exist_ok=True)
            csv_path = output_dir / Path(config.output_csv).name
            if config.fault_events_json:
                fault_json_path = output_dir / Path(config.fault_events_json).name
            else:
                fault_json_path = None
        else:
            csv_path = config.output_csv
            fault_json_path = config.fault_events_json
        
        if args.verbose:
            print(f"Exporting results to {csv_path}...")
        
        # Export metrics
        metrics = client.metrics()
        metrics.export_csv(str(csv_path), granularity_s=config.granularity_s)
        
        # Export fault events
        if fault_json_path:
            fault_tracker.export_json(str(fault_json_path))
        
        if args.verbose:
            print("✓ Results exported")
            
            # Print summary
            summary = metrics.summary()
            print(f"\nSummary:")
            print(f"  Total requests: {summary.total}")
            print(f"  Succeeded: {summary.succeeded} ({summary.succeeded/summary.total*100:.1f}%)")
            print(f"  Dropped (queue): {summary.dropped_queue}")
            print(f"  Dropped (deadline): {summary.dropped_deadline}")
            print(f"  Dropped (failure): {summary.dropped_server_failure}")
            print(f"  Mean latency: {summary.mean:.2f}ms")
            print(f"  P50 latency: {summary.p50:.2f}ms")
            print(f"  P99 latency: {summary.p99:.2f}ms")
            print(f"  Retries per request: {summary.retries_per_root:.2f}")
        else:
            print(f"✓ Results written to {csv_path}")
        
    except Exception as e:
        print(f"❌ Error exporting results: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
