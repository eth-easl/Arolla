import argparse
import glob
import os
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

def load_data(input_dir):
    # Match specific pattern for reproducibility
    files = glob.glob(os.path.join(input_dir, "output_*.csv"))
    clients = {}
    for f in files:
        # Extract client name from filename: output_CLIENTNAME.csv
        basename = os.path.basename(f)
        if basename == "output.csv":
             continue # Skip default if specific exists? Or name it "default"
        
        client_name = basename.replace("output_", "").replace(".csv", "")
        df = pd.read_csv(f)
        clients[client_name] = df
    return clients

def plot_reproduction(clients, output_dir):
    # Palette matching the blog/user image
    palette = {
        'retry-3x': 'tab:blue', 
        'retry-2x': 'tab:blue',
        'jittered': 'tab:red'
    }

    # Setup Grid: 1 Row x 2 Cols (Server Uptime, RPS Amplification)
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    
    # Common settings
    outage_start = 0.5
    outage_end = 1.0
    
    # 1. Server Uptime (%)
    ax = axes[0]
    for name, df in clients.items():
        if df is None: continue
        # Calculate success rate per bucket (Uptime)
        # Attempts = Root + Retries
        attempts = df['root_requests'] + df['retries']
        # Failures = Root Failure + Retry Failure (Instantaneous counts)
        failures = df['failure_root'] + df['failure_retry']
        
        # Avoid div by zero
        success_rate = (attempts - failures) / attempts
        success_rate = success_rate.fillna(1.0) # If no attempts, assume 100% uptime?
        
        ax.plot(df['timepoint'], success_rate * 100, label=name, color=palette.get(name, 'black'), linewidth=2)
        
    ax.set_title("Server Uptime")
    ax.set_ylabel("Server Uptime (%)")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(-5, 105)
    # Add vertical lines for outage
    ax.vlines([outage_start, outage_end], 0, 100, colors='gray', linestyles='dashed', alpha=0.5)

    # 2. Server RPS Amplification
    ax = axes[1]
    TARGET_RPS = 100 # From YAML
    
    for name, df in clients.items():
        if df is None: continue
        # total_request is cumulative! Use root_requests + retries for instantaneous rate.
        # Check if columns exist
        if 'root_requests' in df.columns and 'retries' in df.columns:
            attempts = df['root_requests'] + df['retries']
        else:
            # Fallback (though we know they exist from previous steps)
            attempts = df['total_attempts'] if 'total_attempts' in df.columns else df['total_request']
            
        # Granularity is 0.01s (from yaml) determine it from data?
        granularity = df['timepoint'].diff().mode()[0] if len(df) > 1 else 0.01
        
        rps = attempts / granularity
        rel_rps = (rps / TARGET_RPS) * 100
        
        ax.plot(df['timepoint'], rel_rps, label=name, color=palette.get(name, 'black'), linewidth=2)
        
    ax.set_title("Server RPS Amplification")
    ax.set_ylabel("Relative Server RPS (%)")
    ax.set_xlabel("Time (s)")
    # Add vertical lines for outage
    ax.vlines([outage_start, outage_end], 0, 400, colors='gray', linestyles='dashed', alpha=0.5)
    
    # Legend
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(0.5, 1.05), ncol=2)
    
    plt.tight_layout()
    output_path = os.path.join(output_dir, "reproduced_figures_3x.png")
    plt.savefig(output_path, bbox_inches='tight')
    print(f"Saved to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir", help="Directory containing CSV files")
    parser.add_argument("-o", "--output", help="Output directory (default: input_dir/plots)")
    args = parser.parse_args()
    
    output_dir = args.output if args.output else os.path.join(args.input_dir, "plots")
    os.makedirs(output_dir, exist_ok=True)
    
    clients = load_data(args.input_dir)
    plot_reproduction(clients, output_dir)
