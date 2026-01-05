#!/usr/bin/env python3
import glob
import os
import re
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

def find_latest_results(base_path="results"):
    # Pattern: N{n}_M{m} (and optionally timestamp if present, but we look for the dir name)
    # Update to search recursively for directories matching N*_M*
    
    # If base_path is a specific batch dir (e.g. results/diversity_20260101...), search inside it.
    # If base_path is "results", search deeper.
    
    print(f"Searching for results in {base_path}...")
    experiments = {} # (n, m) -> path
    
    # Walk through directory
    for root, dirs, files in os.walk(base_path):
        for dirname in dirs:
            # Check if dir matches N*_M* pattern
            # Matches N100_M10 or N100_M10_2026...
            # The new format is N100_M10 inside a batch folder, so basename is N100_M10.
            # Old format was N100_M10_TIMESTAMP.
            
            # Regex to handle both: Start with N\d+_M\d+
            match = re.match(r"^N(\d+)_M(\d+)(?:_(\d+))?$", dirname)
            if match:
                n = int(match.group(1))
                m = int(match.group(2))
                
                # If timestamp is present in name (group 3), use it for sorting version.
                # If not (nested inside batch), use batch dir timestamp?
                # Simplify: Just keep track of all found, and if multiple, pick latest file modification?
                
                path = os.path.join(root, dirname)
                # Check for output.csv
                if not os.path.exists(os.path.join(path, "output.csv")):
                    continue
                    
                key = (n, m)
                # Logic to prefer latest:
                # If we have a duplicate (n, m), check timestamps.
                # If strictly needed, compare mtimes.
                
                path_mtime = os.path.getmtime(path)
                
                if key not in experiments:
                    experiments[key] = (path, path_mtime)
                else:
                    if path_mtime > experiments[key][1]:
                         experiments[key] = (path, path_mtime)

    # Unwrap tuple
    final_experiments = {k: v[0] for k, v in experiments.items()}
    print(f"Found {len(final_experiments)} unique experiments.")
    return final_experiments

def calculate_ttr(csv_path: str, spike_end: float = 40.0, threshold: float = 0.99, window: int = 5):
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"Error reading {csv_path}: {e}")
        return None

    # Calculate success rate
    # success_root vs root_requests
    # Avoid div by zero
    df['success_rate'] = df.apply(lambda row: row['success_root'] / row['root_requests'] if row['root_requests'] > 0 else 1.0, axis=1)
    
    # Filter after spike
    post_spike = df[df['timepoint'] > spike_end].copy()
    if post_spike.empty:
        return None
        
    # Moving average to smooth noise
    # Assumes 1s granularity
    post_spike['smoothed_success'] = post_spike['success_rate'].rolling(window=window, min_periods=1).mean()
    
    # Find recovery point
    # Recovery = point where smoothed success stays > threshold until end?
    # Or just first time it crosses?
    # Metastability means it might NOT cross.
    
    # Let's find first time it crosses threshold
    recovered_rows = post_spike[post_spike['smoothed_success'] >= threshold]
    
    if recovered_rows.empty:
        # Did not recover
        return None # Effectively infinity
        
    first_recovery_time = recovered_rows.iloc[0]['timepoint']
    
    # Verify stability? (Optional)
    # Check if it drops below threshold again significantly?
    # For now, simplistic TTR
    
    ttr = first_recovery_time - spike_end
    return ttr

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Plot TTR vs Diversity")
    parser.add_argument("results_dir", nargs="?", default="results", help="Directory containing experiment results")
    args = parser.parse_args()
    
    experiments = find_latest_results(args.results_dir)
    data = []
    
    for (n, m), path in experiments.items():
        csv_path = os.path.join(path, "output.csv")
        if not os.path.exists(csv_path):
            continue
            
        ttr = calculate_ttr(csv_path)
        
        # If None, it means TTR > observation window (170s)
        # Use a max value for plotting?
        if ttr is None:
            ttr = 200.0 # Saturation
            
        data.append({"N": n, "M": m, "TTR": ttr})
        print(f"N={n}, M={m}: TTR={ttr:.2f}s")
        
    df = pd.DataFrame(data)
    if df.empty:
        print("No data found.")
        return

    # Plot
    plt.figure(figsize=(10, 6))
    
    # Sort data
    df = df.sort_values(['M', 'N'])
    
    # Define styles matching Figure 2
    # M=1 (Low Diversity) -> Solid Black
    # M=10 (Medium Diversity) -> Dashed Grey
    # M=100 (High Diversity) -> Dotted Light Grey
    
    styles = {
        1: {'color': 'black', 'ls': '-', 'label': 'Low Diversity (M=1)', 'lw': 2.5},
        10: {'color': 'blue', 'ls': '--', 'label': 'Medium Diversity (M=10)', 'lw': 2.5},
        100: {'color': 'green', 'ls': ':', 'label': 'High Diversity (M=100)', 'lw': 2.5}
    }
    
    # # Shade the "Noise Floor" region (N > 1000)
    # plt.axvspan(1000, 10000, color='lightgrey', alpha=0.5, zorder=0)
    # plt.axvline(x=1000, color='black', linestyle='--', linewidth=1)
    
    # # Annotations
    # plt.text(1200, 300, "Exponential TTR\nGrowth due to\n\"Noise Floor\"", 
    #          fontsize=12, verticalalignment='center')
             
    # plt.text(400, 400, "Phase Shift\nThreshold\n(N=1000)", 
    #          fontsize=10, horizontalalignment='center')

    # Plot lines
    for m in sorted(df['M'].unique()):
        subset = df[df['M'] == m]
        style = styles.get(m, {'color': 'blue', 'ls': '-', 'label': f'M={m}', 'lw': 1})
        
        plt.plot(subset['N'], subset['TTR'], 
                 label=style['label'], 
                 marker='o', # Markers needed for single points
                 color=style['color'],
                 linestyle=style['ls'],
                 linewidth=style['lw'])

    # Axes
    plt.xscale('log')
    # plt.yscale('log')
    
    plt.xlabel('Number of Clients (N) [log scale]', fontsize=14)
    plt.ylabel('Time to Recovery (TTR)\n[log scale]', fontsize=14)
    
    # Limits to match figure
    plt.xlim(1, 200) # Adjusted for N=1 to N=100 (log scale needs breathing room)
    plt.ylim(10, 200)    # Adjusted for TTR range observed (34-77s)
    
    # Add arrow for Diversity Penalty
    # Arrow from M=1 line to M=100 line
    # M=1 approx 41s, M=10 approx 77s/41s
    # plt.annotate("Diversity\nPenalty", 
    #              xy=(50, 45), # Tail (near M=1 or M=100)
    #              xytext=(60, 25), # Text location
    #              arrowprops=dict(facecolor='black', shrink=0.05, width=1, headwidth=8),
    #              fontsize=12)

    # Clean up
    # plt.title('Figure 2: TTR Sensitivity to Client Diversity (N vs M)', fontsize=14, weight='bold', y=-0.2)
    
    # Legend manually near lines
    start_n = 1.1 # Near start
    for m in styles:
        row = df[(df['M'] == m) & (df['N'] == 1)] # Use N=1 as anchor
        if not row.empty:
            y_val = row.iloc[0]['TTR']
            label = styles[m]['label']
            # Offset labels slightly to avoid overlap
            offset = 1.0
            if m == 10: offset = 0.8
            elif m == 100: offset = 1.2
            
            plt.text(start_n, y_val * offset, label, fontsize=10, color=styles[m]['color'])

    # Grid (Ticks only on left/bottom frame like typical academic plots)
    # Turn off top/right spines
    # plt.gca().spines['top'].set_visible(False)
    # plt.gca().spines['right'].set_visible(False)
    
    plt.tight_layout()
    
    # Save to results directory
    out_path = os.path.join(args.results_dir, "diversity_ttr_plot.png")
    plt.savefig(out_path, bbox_inches='tight')
    print(f"Plot saved to {out_path}")

if __name__ == "__main__":
    main()
