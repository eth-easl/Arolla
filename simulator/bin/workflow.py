#!/usr/bin/env python3
"""
Run a complete experiment workflow: simulation + plotting.

Automatically organizes outputs by scenario name (from YAML filename).
"""

import argparse
import sys
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

# Add src to path
import sys
from pathlib import Path
script_dir = Path(__file__).parent.resolve()
src_dir = script_dir.parent / "src"
sys.path.append(str(src_dir))

from simulator.config.loader import ConfigLoader


def run_workflow(yaml_file: str, output_base: str = "results", 
                 verbose: bool = False, plot: bool = True,
                 time_range: str = None, plotting_script: str = None):
    """
    Run complete experiment workflow.
    
    Args:
        yaml_file: Path to YAML experiment configuration
        output_base: Base directory for all results
        verbose: Enable verbose output
        plot: Generate plots after simulation
        time_range: Optional time range for plots (e.g., "0-60")
    
    Returns:
        0 on success, 1 on failure
    """
    yaml_path = Path(yaml_file)
    script_dir = Path(__file__).parent.resolve()
    
    # Validate YAML file exists
    if not yaml_path.exists():
        print(f"Error: YAML file not found: {yaml_file}")
        return 1
    
    # Extract scenario name from YAML filename (without .yaml extension)
    scenario_name = yaml_path.stem
    
    # Create timestamped output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(output_base) / f"{scenario_name}_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Archive config file
    shutil.copy(yaml_path, output_dir / yaml_path.name)
    
    shutil.copy(yaml_path, output_dir / yaml_path.name)
    
    plots_dir = output_dir / "plots"
    
    print("=" * 70)
    print(f"Running Experiment Workflow: {scenario_name}")
    print("=" * 70)
    print(f"YAML config: {yaml_file}")
    print(f"Output directory: {output_dir}")
    print()
    
     # Step 0: Check for sweeps
    print("-" * 70)
    ran_sweep = False
    
    # Load config to check for sweeps (inline or legacy)
    try:
         # Import detection logic from run_sweep to ensure consistency
         import importlib.util
         spec = importlib.util.spec_from_file_location("run_sweep", script_dir / "run_sweep.py")
         run_sweep_module = importlib.util.module_from_spec(spec)
         spec.loader.exec_module(run_sweep_module)
         
         # Load raw yaml to check for lists
         import yaml
         with open(yaml_file, 'r') as f:
             raw_config = yaml.safe_load(f)
             
         sweeps = run_sweep_module.find_sweeps(raw_config)
         
         # Also check legacy sweeps field for backward compat (though we removed it from plan)
         legacy_sweeps = raw_config.get('sweeps')
         
         if sweeps or legacy_sweeps:
             count = len(sweeps) if sweeps else len(legacy_sweeps)
             print(f"Detected {count} sweep dimensions. Switching to Sweep Mode.")
             print(f"Delegating to bin/run_sweep.py...")
             
             run_sweep_script = script_dir / "run_sweep.py"
             cmd = [
                sys.executable,
                str(run_sweep_script),
                str(yaml_file),
                "--target-dir", str(output_dir)
             ]
             if verbose:
                 cmd.append("--verbose")
                 
             try:
                subprocess.run(cmd, check=True)
                ran_sweep = True
             except subprocess.CalledProcessError as e:
                print(f"\n❌ Sweep failed with exit code {e.returncode}")
                return 1
    except Exception as e:
         print(f"Warning: Could not check for sweeps: {e}")

    # Step 1: Run simulation (only if not running sweep)
    if not ran_sweep:
        print("Step 1/2: Running simulation...")
        print("-" * 70)
        
        # Locate run_experiment.py relative to this script (workflow.py)
        # Both are in the same 'bin' directory
        run_experiment_script = script_dir / "run_experiment.py"

        cmd = [
            sys.executable,
            str(run_experiment_script),
            str(yaml_file),
            "--output", str(output_dir)
        ]
        
        if verbose:
            cmd.append("--verbose")
        
        try:
            result = subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"\n❌ Simulation failed with exit code {e.returncode}")
            return 1
        
        print(f"\n✓ Simulation completed")
        # print(f"  CSV output: {csv_file}") # csv_file depends on run now
    else:
        print("Skipping single simulation step (Sweep Mode active).")
    
    # Step 2: Generate plots
    if plot:
        print(f"\nStep 2/2: Generating plots...")
        print("-" * 70)
        
        # Load config to check for custom plotting script
        try:
             config = ConfigLoader.load_from_file(yaml_file)
        except Exception as e:
             print(f"Warning: Could not load YAML to check for plotting script: {e}")
             config = None
             
        custom_script = None
        if config and config.plotting_script:
             custom_script = config.plotting_script
        
        # CLI override
        if plotting_script:
             custom_script = plotting_script
             
        if custom_script:
             print(f"Detected custom plotting script: {custom_script}")
             cmd = [
                sys.executable,
                custom_script,
                str(output_dir), # Pass output dir as first arg
                "-o", str(plots_dir)
             ]
             try:
                subprocess.run(cmd, check=True)
                print(f"\n✓ Custom plots generated in {plots_dir}/")
             except subprocess.CalledProcessError as e:
                print(f"\n⚠ Custom plotting failed with exit code {e.returncode}")
                # Fallback to default plotting? No, better to stop or let user know.
             
             # Return early or continue? 
             # Let's return early as custom script likely replaces default plots.
             # Or maybe user wants BOTH? Usually replacement.
             # Summary
             print()
             print("=" * 70)
             print("✅ Workflow Complete!")
             print("=" * 70)
             print(f"Scenario: {scenario_name}")
             print(f"Output directory: {output_dir}")
             print(f"  - output.csv (simulation data)")
             print(f"  - plots/ (visualizations)")
             print()
             print("Next steps:")
             print(f"  # View plots")
             return 0

        fault_events_file = output_dir / "fault_events.json"
        print(f"\nStep 2/2: Generating plots...")
        print("-" * 70)
        
        fault_events_file = output_dir / "fault_events.json"
        
        # Find all CSV files in output dir
        all_csvs = list(output_dir.glob("*.csv"))
        
        if not all_csvs:
             print(f"Error: No output CSVs found in {output_dir}")
             return 1
             
        # Case 1: Single Client (Legacy) - Exactly one CSV found
        if len(all_csvs) == 1:
            csv_file = all_csvs[0]
            print(f"Detected single-client output: {csv_file.name}")
            cmd = [
                sys.executable,
                "plotting/plot_all.py",
                str(csv_file),
                "-o", str(plots_dir)
            ]
            
            # Add fault events if file exists
            if fault_events_file.exists():
                cmd.extend(["--fault-events", str(fault_events_file)])
            
            if time_range:
                cmd.extend(["--time-range", time_range])

            try:
                subprocess.run(cmd, check=True)
                print(f"\n✓ Plots generated in {plots_dir}/")
            except subprocess.CalledProcessError as e:
                print(f"\n⚠ Plotting failed with exit code {e.returncode}")
        
        # Case 2: Multi-Client - Multiple CSVs found
        else:
            print(f"Detected multi-client output ({len(all_csvs)} files).")
            print("Running comparison plots...")
            
            cmd = [
                sys.executable,
                "plotting/compare_clients.py",
                str(output_dir),
                "-o", str(plots_dir)
            ]
            
            try:
                subprocess.run(cmd, check=True)
                print(f"\n✓ Comparison plots generated in {plots_dir}/")
            except subprocess.CalledProcessError as e:
                print(f"\n⚠ Plotting failed with exit code {e.returncode}")
    else:
        print(f"\nStep 2/2: Skipping plots (--no-plot)")

    
    # Summary
    print()
    print("=" * 70)
    print("✅ Workflow Complete!")
    print("=" * 70)
    print(f"Scenario: {scenario_name}")
    print(f"Output directory: {output_dir}")
    print(f"  - output.csv (simulation data)")
    if plot:
        print(f"  - plots/ (visualizations)")
    print()
    print("Next steps:")
    print(f"  # View plots")
    print(f"  # Analyze data")
    # Try to find what CSVs were generated for the hint
    found_csvs = list(output_dir.glob("*.csv"))
    if len(found_csvs) == 1:
            print(f"  python -c \"import pandas as pd; df = pd.read_csv('{found_csvs[0]}'); print(df.describe())\"")
    elif len(found_csvs) > 1:
            print(f"  # (Multi-client output files are in {output_dir})")
    else:
            print(f"  # (No CSV output found)")
    print()
    
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Run experiment workflow: simulation + plotting',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Run default experiment with plots
  python bin/workflow.py experiments/yaml/default.yaml
  
  # Custom output location
  python bin/workflow.py experiments/yaml/default.yaml -o my_results/
  
  # Skip plotting
  python bin/workflow.py experiments/yaml/default.yaml --no-plot
  
  # Plot specific time range
  python bin/workflow.py experiments/yaml/default.yaml --time-range 0-60
  
  # Verbose output
  python bin/workflow.py experiments/yaml/default.yaml --verbose

Output Structure:
  results/
  └── {scenario}_{timestamp}/
      ├── output.csv          # Simulation data
      └── plots/              # Visualizations
          ├── latency.png
          ├── qps.png
          ├── failures.png
          └── success_rate.png
        '''
    )
    
    parser.add_argument('yaml_file', 
                       help='YAML experiment configuration file')
    parser.add_argument('-o', '--output', default='results',
                       help='Base output directory (default: results/)')
    parser.add_argument('--verbose', action='store_true',
                       help='Enable verbose simulation output')
    parser.add_argument('--no-plot', action='store_true',
                       help='Skip plot generation')
    parser.add_argument('--time-range',
                       help='Time range for plots (e.g., "0-60")')
    parser.add_argument('--plotting-script',
                       help='Path to custom plotting script (overrides YAML)')
    
    args = parser.parse_args()
    
    return run_workflow(
        args.yaml_file,
        output_base=args.output,
        verbose=args.verbose,
        plot=not args.no_plot,
        time_range=args.time_range,
        plotting_script=args.plotting_script
    )


if __name__ == '__main__':
    sys.exit(main())
