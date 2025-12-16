import json
import matplotlib.pyplot as plt
import sys
import os

def plot_results(summary_file):
    print(f"Plotting results from {summary_file}...")
    
    with open(summary_file, 'r') as f:
        data = json.load(f)
    
    #Sort data by RPS just in case
    data.sort(key=lambda x: x['rps'])
    
    rps = [d['rps'] for d in data]
    p50 = [d['p50'] for d in data]
    p99 = [d['p99'] for d in data]
    
    plt.figure(figsize=(10, 6))
    plt.plot(rps, p50, marker='o', label='P50 Latency')
    plt.plot(rps, p99, marker='s', label='P99 Latency')
    
    plt.xlabel('Requests Per Second (RPS)')
    plt.ylabel('Latency (ms)')
    plt.title('RPS vs Latency (P50 & P99)')
    plt.legend()
    plt.grid(True, linestyle='--', alpha=0.7)
    
    # Save plot in the same directory as the summary file
    output_dir = os.path.dirname(summary_file)
    output_file = os.path.join(output_dir, 'rps_vs_latency_plot.png')
    plt.savefig(output_file)
    print(f"Plot saved to {output_file}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 plot_sweep_results.py <path_to_summary.json>")
        # Try to find the latest sweep result automatically
        base_dir = "results"
        if os.path.exists(base_dir):
            sweeps = sorted([os.path.join(base_dir, d) for d in os.listdir(base_dir) if d.startswith("sweep_")])
            if sweeps:
                latest_summary = os.path.join(sweeps[-1], "summary.json")
                if os.path.exists(latest_summary):
                    print(f"No file specified. Using latest found: {latest_summary}")
                    plot_results(latest_summary)
                else:
                    print(f"Latest sweep dir found but no summary.json in {sweeps[-1]}")
            else:
                print("No sweep results found in results/")
        else:
            print("Results directory not found.")
    else:
        plot_results(sys.argv[1])
