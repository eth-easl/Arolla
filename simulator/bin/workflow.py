#!/usr/bin/env python3
"""
Run a complete experiment workflow: simulation + plotting.

Automatically organizes outputs by scenario name (from YAML filename).
"""

import argparse
import sys
import subprocess
from pathlib import Path
from datetime import datetime


def run_workflow(yaml_file: str, output_base: str = "results", 
                 verbose: bool = False, plot: bool = True,
                 time_range: str = None):
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
    
    csv_file = output_dir / "output.csv"
    plots_dir = output_dir / "plots"
    
    print("=" * 70)
    print(f"Running Experiment Workflow: {scenario_name}")
    print("=" * 70)
    print(f"YAML config: {yaml_file}")
    print(f"Output directory: {output_dir}")
    print()
    
    # Step 1: Run simulation
    print("Step 1/2: Running simulation...")
    print("-" * 70)
    
    cmd = [
        sys.executable,
        "bin/run_experiment.py",
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
    print(f"  CSV output: {csv_file}")
    
    # Step 2: Generate plots
    if plot:
        print(f"\nStep 2/2: Generating plots...")
        print("-" * 70)
        
        cmd = [
            sys.executable,
            "plotting/plot_all.py",
            str(csv_file),
            "-o", str(plots_dir)
        ]
        
        if time_range:
            cmd.extend(["--time-range", time_range])
        
        try:
            result = subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"\n⚠ Plotting failed with exit code {e.returncode}")
            print("  (Simulation data is still available)")
        else:
            print(f"\n✓ Plots generated in {plots_dir}/")
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
    if plot:
        print(f"  open {plots_dir}/*.png")
    print(f"  # Analyze data")
    print(f"  python -c \"import pandas as pd; df = pd.read_csv('{csv_file}'); print(df.describe())\"")
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
    
    args = parser.parse_args()
    
    return run_workflow(
        args.yaml_file,
        output_base=args.output,
        verbose=args.verbose,
        plot=not args.no_plot,
        time_range=args.time_range
    )


if __name__ == '__main__':
    sys.exit(main())
