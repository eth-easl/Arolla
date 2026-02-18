#!/usr/bin/env python3
"""
Run parameter sweeps defined in YAML configuration using inline lists.

Detects lists in the configuration, groups them by object identity (for lockstep sweeps via YAML anchors),
and runs the Cartesian product of combinations.
"""

import argparse
import sys
import yaml
import copy
import itertools
import pandas as pd
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Any, Generator, Tuple

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from simulator.config.schema import ExperimentConfig
from simulator.config.loader import ConfigLoader
from simulator.utils.time import s_to_ns

def set_nested_value(data: Dict[str, Any], path: str, value: Any):
    """Set value in nested dictionary using dot notation"""
    parts = path.split('.')
    current = data
    for part in parts[:-1]:
        if part.isdigit():
            part = int(part)
        
        # Handle list indices
        if isinstance(current, list):
            part = int(part)
        
        current = current[part]
    
    last_part = parts[-1]
    if isinstance(current, list):
        last_part = int(last_part)
        current[last_part] = value
    else:
        current[last_part] = value

def find_sweeps(config_dict: Dict[str, Any], prefix: str = "") -> Dict[int, Dict[str, Any]]:
    """
    Recursively find lists in the config dictionary.
    Returns a dict mapping id(list_obj) -> metadata dict.
    Metadata dict: {
        'paths': [path1, path2], # List of paths pointing to this object
        'values': list_obj       # The list of values
    }
    """
    sweeps = {}
    
    # helper for recursion
    def recurse(current, current_path):
        if isinstance(current, dict):
            for k, v in current.items():
                new_path = f"{current_path}.{k}" if current_path else k
                recurse(v, new_path)
        elif isinstance(current, list):
            # Check if this list is a leaf value (sweep) or valid structural list
            # Heuristic: If list contains simple types (int, float, str), it might be a sweep.
            # However, 'services' is a list of dicts. We shouldn't sweep that unless the user intends.
            # But here we are looking for p_fail: [0.1, 0.2].
            # So if elements are dicts, we recurse INTO them.
            if len(current) > 0 and isinstance(current[0], dict):
                for i, item in enumerate(current):
                    new_path = f"{current_path}.{i}"
                    recurse(item, new_path)
            else:
                # Potential sweep. 
                # Be careful: [1, 2] could be a fixed config value.
                # But in our schema, most lists are object lists. 
                # Scalars wrapped in list are likely sweeps if the field expects scalar.
                # Since we don't have easy access to schema type per field here without complexity,
                # we assume lists of scalars are sweeps if they are in "sweepable" fields or generally.
                # Or we rely on the user to only put lists where they want sweeps.
                # Exception: 'parameters' list in old SweepConfig (but that's gone).
                
                # Check for explicit excluded fields or logic?
                # For now, treat all lists of scalars as sweeps.
                if len(current) > 0 and not isinstance(current[0], (dict, list)):
                   obj_id = id(current)
                   if obj_id not in sweeps:
                       sweeps[obj_id] = {'paths': [], 'values': current}
                   sweeps[obj_id]['paths'].append(current_path)

    recurse(config_dict, prefix)
    return sweeps

def run_simulation_instance(config: ExperimentConfig, output_dir: Path, verbose: bool = False) -> List[Dict[str, Any]]:
    """Run a single simulation and return summary metrics per client"""
    
    # Build simulation
    try:
        sim, clients, workloads, fault_tracker, services = ConfigLoader.build_simulation(config)
    except Exception as e:
        print(f"Error building simulation: {e}", file=sys.stderr)
        return []

    # Run simulation
    try:
        max_duration = max(wl.duration_s for wl in workloads) if workloads else 0
        
        # Drive workloads
        for client, workload in zip(clients, workloads):
            workload.drive(sim, lambda s, c=client: c.start_request(s))
        
        sim.run(until=s_to_ns(max_duration))
        sim.run() # Drain
        
        # Collect metrics and merge CSVs by client base name
        results = []
        
        # Group clients by base name (e.g. "client.0" -> "client")
        clients_by_base = defaultdict(list)
        for client in clients:
            # Check if name ends with .<digits> usually added by replicas logic
            parts = client.cfg.name.rsplit('.', 1)
            if len(parts) == 2 and parts[1].isdigit():
                base_name = parts[0]
                replica_id = int(parts[1])
            else:
                base_name = client.cfg.name
                replica_id = 0
                
            clients_by_base[base_name].append((client, replica_id))

        for base_name, group in clients_by_base.items():
            # Determine if we should merge
            # If group size > 1, we merge.
            if len(group) > 1:
                 dfs = []
                 for client, rid in group:
                     try:
                         df = client.metrics().to_dataframe()
                         if not df.empty:
                             df['replica_id'] = rid
                             dfs.append(df)
                     except Exception as e:
                         print(f"Error getting metrics for {client.cfg.name}: {e}")
                 
                 if dfs:
                     merged_df = pd.concat(dfs, ignore_index=True)
                     csv_path = output_dir / f"output_{base_name}_merged.csv"
                     merged_df.to_csv(csv_path, index=False)
                 else:
                     csv_path = None
                 
                 # Create results entries pointing to the merged file
                 for client, rid in group:
                     metrics = client.metrics()
                     summary = metrics.summary()
                     goodput = summary.succeeded / max_duration if max_duration > 0 else 0
                     
                     res = {
                        "client_name": base_name, # Use base name for grouping
                        "replica_id": rid,
                        "total_requests": summary.total,
                        "total_attempts": summary.attempts_total,
                        "success_rate": (summary.succeeded / summary.total) if summary.total > 0 else 0.0,
                        "mean_latency_ms": summary.mean,
                        "p50_latency_ms": summary.p50,
                        "p99_latency_ms": summary.p99,
                        "goodput_rps": goodput,
                        "raw_csv_path": str(csv_path) if csv_path else "" 
                     }
                     results.append(res)
                     
            else:
                # Single client fallback
                client, rid = group[0]
                metrics = client.metrics()
                csv_path = output_dir / f"output_{client.cfg.name}.csv"
                metrics.export_csv(str(csv_path))
                
                summary = metrics.summary()
                goodput = summary.succeeded / max_duration if max_duration > 0 else 0
                
                res = {
                    "client_name": base_name,
                    "replica_id": rid,
                    "total_requests": summary.total,
                    "total_attempts": summary.attempts_total,
                    "success_rate": (summary.succeeded / summary.total) if summary.total > 0 else 0.0,
                    "mean_latency_ms": summary.mean,
                    "p50_latency_ms": summary.p50,
                    "p99_latency_ms": summary.p99,
                    "goodput_rps": goodput,
                    "raw_csv_path": str(csv_path)
                }
                results.append(res)

        # Fault events
        if fault_tracker:
             # simple json dump if fault_tracker supports it
             pass 

    except Exception as e:
        print(f"Error running simulation: {e}", file=sys.stderr)
        return []
        
    return results

def main():
    parser = argparse.ArgumentParser(description="Run parameter sweeps")
    parser.add_argument("config", help="Path to YAML configuration file")
    # Change default output to results_sweep as requested
    parser.add_argument("--output", default="results_sweep", help="Base Output directory")
    parser.add_argument("--target-dir", help="Specific output directory (bypasses timestamp folder creation)")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    # 1. Load Raw YAML
    yaml_path = Path(args.config)
    with open(yaml_path, 'r') as f:
        raw_config = yaml.safe_load(f)
        
    # 2. Detect Sweeps
    raw_sweeps = find_sweeps(raw_config)
    
    if not raw_sweeps:
        print("No inline parameter sweeps (lists) detected.")
        return 0
        
    print(f"Found {len(raw_sweeps)} sweep dimensions (groups of parameters).")
    
    # Prepare Sweep Combinations
    # raw_sweeps values are {obj_id: {paths: [...], values: [...]}}
    # We turn this into list of dimensions
    sweep_dimensions = []
    for meta in raw_sweeps.values():
        sweep_dimensions.append(meta)
        
    sweep_values_list = [d['values'] for d in sweep_dimensions]
    
    # Cartesian product
    combinations = list(itertools.product(*sweep_values_list))
    print(f"Total combinations to run: {len(combinations)}")
    
    # Prepare output
    if args.target_dir:
        run_dir = Path(args.target_dir)
    else:
        output_dir = Path(args.output)
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        run_dir = output_dir / f"{yaml_path.stem}_sweep_{timestamp}"
    
    run_dir.mkdir(parents=True, exist_ok=True)
    
    all_results = []
    
    # 3. Run Loop
    for i, combo in enumerate(combinations):
        current_values = {}
        # Map combination values back to paths
        # combo is tuple of values e.g. (0.1, 5) corresponding to sweep_dimensions order
        
        # Create a meaningful name for this run
        run_name_parts = []
        
        for dim, val in zip(sweep_dimensions, combo):
            # Use the first path as the representative name
            # shorten path? services.0.partial_failures.0.p_fail -> p_fail
            ref_path = dim['paths'][0]
            short_name = ref_path.split('.')[-1] 
            run_name_parts.append(f"{short_name}_{val}")
            
            for path in dim['paths']:
                current_values[path] = val
        
        run_subdir_name = "run_" + "_".join(run_name_parts)
        run_output_dir = run_dir / run_subdir_name
        run_output_dir.mkdir(exist_ok=True)

        print(f"[{i+1}/{len(combinations)}] Running {run_subdir_name}...")
        if args.verbose:
             print(f"  Overrides: {current_values}")
        
        # Deep copy raw config
        current_config_dict = copy.deepcopy(raw_config)
        
        # Apply overrides
        for path, value in current_values.items():
            try:
                set_nested_value(current_config_dict, path, value)
            except Exception as e:
                print(f"Error setting {path}: {e}")
        
        # Create config object
        try:
            # Clean up potential artifacts from dict
            if 'sweeps' in current_config_dict:
                del current_config_dict['sweeps']
                
            config_obj = ExperimentConfig(**current_config_dict)
        except Exception as e:
            print(f"Skipping invalid configuration: {e}")
            continue
            
        # Run
        results = run_simulation_instance(config_obj, run_output_dir, verbose=args.verbose)
        
        # Append sweep params to results
        for res in results:
            res.update(current_values)
            res['run_dir'] = str(run_output_dir) # Track where artifacts are
            all_results.append(res)
            
    # 4. Save Summary & Reorganize
    if not all_results:
        print("No results collected.")
        return 1
        
    df = pd.DataFrame(all_results)
    
    # 4a. Organize by client
    import shutil
    by_client_dir = run_dir / "by_client"
    by_client_dir.mkdir(exist_ok=True)
    
    # Get unique clients
    clients = df["client_name"].unique()
    
    for client in clients:
        client_dir = by_client_dir / client
        client_dir.mkdir(exist_ok=True)
        
        client_df = df[df["client_name"] == client]
        
        # Save split summary
        client_df.drop(columns=["raw_csv_path", "run_dir"], errors="ignore").to_csv(
            run_dir / f"summary_{client}.csv", index=False
        )
        
        # Copy raw CSVs
        copied_destinations = set()
        for _, row in client_df.iterrows():
            if "raw_csv_path" in row and "run_dir" in row:
                raw_path = str(row["raw_csv_path"])
                if not raw_path: 
                    continue
                    
                src = Path(raw_path)
                if src.exists():
                     # Construct meaningful filename from parameters
                     # e.g. p_fail_0.1.csv
                     # We can use the run_dir name which is run_p_fail_0.1
                     run_subdir_name = Path(row["run_dir"]).name
                     # remove 'run_' prefix if exists
                     if run_subdir_name.startswith("run_"):
                         run_subdir_name = run_subdir_name[4:]
                         
                     dest_name = f"{run_subdir_name}.csv"
                     
                     if dest_name in copied_destinations:
                         continue
                         
                     shutil.copy(src, client_dir / dest_name)
                     copied_destinations.add(dest_name)

    # 4b. Cleanup run folders
    # detailed runs are now duplicated in by_client, so we can remove the run_ folders
    for item in run_dir.iterdir():
        if item.is_dir() and item.name.startswith("run_"):
            shutil.rmtree(item)

    # 4c. Main Summary
    summary_path = run_dir / "summary.csv"
    # drop internal columns
    df.drop(columns=["raw_csv_path", "run_dir"], errors="ignore").to_csv(summary_path, index=False)
    
    print(f"\n✓ Sweep completed. Results saved to: {summary_path}")
    print(f"✓ Per-client results organized in: {by_client_dir}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
