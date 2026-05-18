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
            if config.clients:
                 print(f"  Clients: {len(config.clients)}")
            else:
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
        sim, clients, workloads, fault_tracker, services = ConfigLoader.build_simulation(config)
        if args.verbose:
            print(f"✓ Simulation built successfully ({len(clients)} clients)")
    except Exception as e:
        print(f"❌ Error building simulation: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    
    # Run simulation
    try:
        max_duration = max(wl.duration_s for wl in workloads)
        if args.verbose:
            print(f"Running simulation for {max_duration}s...")
        
        # Hook workloads to clients
        for client, workload in zip(clients, workloads):
            workload.drive(sim, lambda s, c=client: c.start_request(s))
        
        # Run until max workload duration
        sim.run(until=s_to_ns(max_duration))
        
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
            csv_path = Path(config.output_csv)
            fault_json_path = config.fault_events_json
        
        if args.verbose:
            print(f"Exporting results...")
        
        # Merge metrics from all clients into a single DataFrame
        import pandas as pd
        
        all_dfs = []
        for client in clients:
            metrics = client.metrics()
            # Use to_dataframe (assuming granularity_s is supported or handled inside)
            # Metrics.to_dataframe creates buckets. 
            # We need to ensure granularity is passed if the method supports it, or use export logic.
            # Checking Collector code previously: export_csv calls bucketing. 
            # I should use the Logic from run_sweep.py which handled this manually or used a helper.
            # Actually, let's look at how run_sweep did it. 
            # It called metrics.to_dataframe(granularity_s).
            
            df = metrics.to_dataframe(granularity_s=config.granularity_s)
            
            # Add client identifier
            # Use config name which includes replica suffix if applicable
            df['client_id'] = client.cfg.name
            
            # Identify base name and replica id
            # Name format: "client" or "client.0"
            if '.' in client.cfg.name and client.cfg.name.split('.')[-1].isdigit():
                parts = client.cfg.name.rsplit('.', 1)
                df['client_name'] = parts[0]
                df['replica_id'] = int(parts[1])
            else:
                df['client_name'] = client.cfg.name
                df['replica_id'] = 0
                
            all_dfs.append(df)
            
            if args.verbose:
                # Print summary for this client
                summary = metrics.summary()
                print(f"\nSummary for {client.cfg.name}:")
                print(f"  Total requests: {summary.total}")
                print(f"  Succeeded: {summary.succeeded} ({summary.succeeded/summary.total*100:.1f}%)" if summary.total > 0 else "  Succeeded: 0 (0.0%)")
                print(f"  Dropped (queue): {summary.dropped_queue}")
                print(f"  Dropped (deadline): {summary.dropped_deadline}")
                print(f"  Dropped (failure): {summary.dropped_server_failure}")
                print(f"  Mean latency: {summary.mean:.2f}ms")
                print(f"  P50 latency: {summary.p50:.2f}ms")
                print(f"  P99 latency: {summary.p99:.2f}ms")
                print(f"  Retries per request: {summary.retries_per_root:.2f}")

        if all_dfs:
            final_df = pd.concat(all_dfs, ignore_index=True)
            
            # Ensure output csv path is strictly what user requested
            # If user said "results/output.csv", we write to "results/output.csv"
            # No suffixes.
            final_df.to_csv(csv_path, index=False)
            
            if args.verbose:
                print(f"  ✓ Exported combined metrics to {csv_path}")
        else:
            print("Warning: No metrics to export.")

        # Export fault events (global)
        if fault_json_path:
            fault_tracker.export_json(str(fault_json_path))
            if args.verbose:
                 print(f"  ✓ Fault events exported to {fault_json_path}")
        
        # Export per-tenant retry admission stats from load limiter
        import json
        admission_stats = {}
        for svc_name, svc_rt in services.items():
            stats = svc_rt.get_admission_stats()
            if stats:
                admission_stats[svc_name] = stats
        if admission_stats:
            stats_path = csv_path.parent / "admission_stats.json"
            with open(stats_path, 'w') as f:
                json.dump(admission_stats, f, indent=2)
            if args.verbose:
                print(f"  ✓ Admission stats exported to {stats_path}")

        # Export per-service metrics if multi-service topology
        if len(services) > 1:
            from simulator.metrics.service_collector import (
                collect_service_metrics,
                collect_service_attempt_events,
            )
            svc_df = collect_service_metrics(services, granularity_s=config.granularity_s)
            if not svc_df.empty:
                svc_csv_path = csv_path.parent / "service_metrics.csv"
                svc_df.to_csv(svc_csv_path, index=False)
                if args.verbose:
                    print(f"  ✓ Per-service metrics exported to {svc_csv_path}")
            attempts_df = collect_service_attempt_events(services)
            if not attempts_df.empty:
                attempts_csv_path = csv_path.parent / "service_attempts.csv"
                attempts_df.to_csv(attempts_csv_path, index=False)
                if args.verbose:
                    print(f"  ✓ Per-service attempts exported to {attempts_csv_path}")
        
    except Exception as e:
        print(f"❌ Error exporting results: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
