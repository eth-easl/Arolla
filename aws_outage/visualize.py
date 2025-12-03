import pandas as pd
import matplotlib.pyplot as plt
import os

METRICS_DIR = "metrics"
CONTROL_PLANE_CSV = os.path.join(METRICS_DIR, "control_plane.csv")
CLIENT_CSV = os.path.join(METRICS_DIR, "client.csv")
EVENTS_CSV = os.path.join(METRICS_DIR, "events.csv")
OUTPUT_FILE = "simulation_results.png"

def load_data():
    # Load Control Plane Data
    try:
        df_cp = pd.read_csv(CONTROL_PLANE_CSV, names=["timestamp", "status_code", "queue_depth", "processing_time"])
        df_cp["timestamp"] = pd.to_datetime(df_cp["timestamp"], unit="s")
        df_cp = df_cp.set_index("timestamp")
    except Exception:
        df_cp = pd.DataFrame()

    # Load Client Data
    try:
        df_client = pd.read_csv(CLIENT_CSV, names=["timestamp", "latency", "status_code"])
        df_client["timestamp"] = pd.to_datetime(df_client["timestamp"], unit="s")
        df_client = df_client.set_index("timestamp")
    except Exception:
        df_client = pd.DataFrame()
    
    # Load DNS Server Data
    try:
        df_dns = pd.read_csv(os.path.join(METRICS_DIR, "dns_server.csv"), names=["timestamp", "status"])
        df_dns["timestamp"] = pd.to_datetime(df_dns["timestamp"], unit="s")
        df_dns = df_dns.set_index("timestamp")
    except Exception:
        df_dns = pd.DataFrame()
        
    # Load Events
    events = {}
    try:
        df_events = pd.read_csv(EVENTS_CSV)
        for _, row in df_events.iterrows():
            events[row["event"]] = pd.to_datetime(row["timestamp"], unit="s")
    except Exception:
        pass
        
    return df_cp, df_client, df_dns, events

def plot_results(df_cp, df_client, df_dns, events):
    fig = plt.figure(figsize=(14, 14))
    gs = fig.add_gridspec(5, 1, hspace=0.3)
    
    ax1 = fig.add_subplot(gs[0, 0])  # Control Plane RPS
    ax2 = fig.add_subplot(gs[1, 0])  # Queue Depth (NEW!)
    ax3 = fig.add_subplot(gs[2, 0])  # DNS Server RPS
    ax4 = fig.add_subplot(gs[3, 0])  # Client Latency
    ax5 = fig.add_subplot(gs[4, 0])  # Client Request Rate
    
    # --- Plot 1: Control Plane RPS (Stacked) ---
    if not df_cp.empty:
        # Resample to 1-second bins
        df_resampled = df_cp.groupby("status_code").resample("1s").size().unstack(level=0, fill_value=0)
        
        # Categorize columns into Success (200) and Fail (500/503)
        success_cols = [c for c in df_resampled.columns if c == 200]
        fail_cols = [c for c in df_resampled.columns if c != 200]
        
        df_plot = pd.DataFrame()
        df_plot["Success"] = df_resampled[success_cols].sum(axis=1) if success_cols else 0
        df_plot["Failure"] = df_resampled[fail_cols].sum(axis=1) if fail_cols else 0
        
        ax1.stackplot(df_plot.index, df_plot["Success"], df_plot["Failure"], 
                      labels=["Success (200 OK)", "Failure (503/Error)"],
                      colors=["#2ca02c", "#d62728"], alpha=0.7)
        
        ax1.set_ylabel("RPS")
        ax1.set_title("Control Plane - Request Rate")
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)

    # --- Plot 2: Queue Depth (NEW!) ---
    if not df_cp.empty and "queue_depth" in df_cp.columns:
        # Resample to show max queue depth per second
        df_queue = df_cp["queue_depth"].resample("1s").max()
        
        ax2.fill_between(df_queue.index, 0, df_queue.values, 
                          color="#e377c2", alpha=0.6, label="Queue Depth")
        ax2.plot(df_queue.index, df_queue.values, 
                 color="#8c564b", linewidth=2)
        
        ax2.set_ylabel("Queued Requests")
        ax2.set_title("Control Plane - Queue Depth (Retry Storm Impact)")
        ax2.legend(loc="upper left")
        ax2.grid(True, alpha=0.3)

    # --- Plot 3: DNS Server RPS ---
    if not df_dns.empty:
        df_dns_resampled = df_dns.groupby("status").resample("1s").size().unstack(level=0, fill_value=0)
        
        # Plot stacked for success vs nxdomain
        success_cols_dns = [c for c in df_dns_resampled.columns if c == "success"]
        fail_cols_dns = [c for c in df_dns_resampled.columns if c == "nxdomain"]
        
        df_dns_plot = pd.DataFrame()
        df_dns_plot["Success"] = df_dns_resampled[success_cols_dns].sum(axis=1) if success_cols_dns else 0
        df_dns_plot["NXDOMAIN"] = df_dns_resampled[fail_cols_dns].sum(axis=1) if fail_cols_dns else 0
        
        ax3.stackplot(df_dns_plot.index, df_dns_plot["Success"], df_dns_plot["NXDOMAIN"],
                      labels=["Success", "NXDOMAIN"],
                      colors=["#1f77b4", "#ff7f0e"], alpha=0.7)
        
        ax3.set_ylabel("RPS")
        ax3.set_title("DNS Server - Query Rate")
        ax3.legend(loc="upper left")
        ax3.grid(True, alpha=0.3)

    # --- Plot 4: Client Latency ---
    if not df_client.empty:
        # Scatter plot for raw latency with color-coded status
        colors = df_client["status_code"].map({200: "green", 503: "red", 502: "red", 0: "orange"}).fillna("blue")
        
        ax4.scatter(df_client.index, df_client["latency"] * 1000, 
                    c=colors, s=10, alpha=0.5)
        
        # Create legend
        from matplotlib.patches import Patch
        legend_elements = [
            Patch(facecolor='green', label='200 OK'),
            Patch(facecolor='red', label='503 Service Unavailable'),
            Patch(facecolor='orange', label='Connection Error')
        ]
        ax4.legend(handles=legend_elements, loc="upper left")
        
        ax4.set_ylabel("Latency (ms)")
        ax4.set_title("Client Latency")
        ax4.grid(True, alpha=0.3)
    
    # --- Plot 5: Client Request Rate ---
    if not df_client.empty:
        df_client_resampled = df_client.resample("1s").size()
        ax5.plot(df_client_resampled.index, df_client_resampled.values, 
                 color="#9467bd", linewidth=2, label="Total Requests")
        
        ax5.set_ylabel("RPS")
        ax5.set_xlabel("Time")
        ax5.set_title("Client - Request Rate")
        ax5.legend(loc="upper left")
        ax5.grid(True, alpha=0.3)

    # --- Overlay Events and Align X-Axis ---
    
    # Calculate global time range
    all_timestamps = []
    if not df_cp.empty: all_timestamps.extend(df_cp.index)
    if not df_client.empty: all_timestamps.extend(df_client.index)
    if not df_dns.empty: all_timestamps.extend(df_dns.index)
    
    if all_timestamps:
        min_time = min(all_timestamps)
        max_time = max(all_timestamps)
        
        # Add some padding
        padding = pd.Timedelta(seconds=2)
        start_lim = min_time - padding
        end_lim = max_time + padding

        for ax in [ax1, ax2, ax3, ax4, ax5]:
            # Set shared X-limits
            ax.set_xlim(start_lim, end_lim)
            
            # Overlay events
            if "DNS_BREAK" in events and "DNS_FIX" in events:
                ax.axvspan(events["DNS_BREAK"], events["DNS_FIX"], color="red", alpha=0.1, label="Outage Window")
                ax.axvline(events["DNS_BREAK"], color="gray", linestyle="--", label="DNS Failure")
                ax.axvline(events["DNS_FIX"], color="gray", linestyle="--", label="DNS Fixed")

    # Save
    plt.tight_layout()
    plt.savefig(OUTPUT_FILE)
    print(f"Saved plot to {OUTPUT_FILE}")

if __name__ == "__main__":
    df_cp, df_client, df_dns, events = load_data()
    plot_results(df_cp, df_client, df_dns, events)
