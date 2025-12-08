import pandas as pd
import matplotlib.pyplot as plt
import os
import numpy as np

# Set style
plt.style.use('ggplot')
plt.rcParams.update({'figure.max_open_warning': 0})

OUTPUT_DIR = "experiment_plots"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

def load_control_plane_data(metrics_dir):
    csv_path = os.path.join(metrics_dir, "control_plane.csv")
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    
    try:
        # New format with headers
        df = pd.read_csv(csv_path, names=["timestamp", "status_code", "queue_depth", "processing_time", "req_id", "attempt", "client_type"])
    except:
        # Fallback to old format
        df = pd.read_csv(csv_path, names=["timestamp", "status_code", "queue_depth", "processing_time"])
        
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    # Calculate start time for Ingress RPS
    df["start_time"] = df["timestamp"] - pd.to_timedelta(df["processing_time"], unit="s")
    return df

def load_dns_data(metrics_dir):
    csv_path = os.path.join(metrics_dir, "dns_server.csv")
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(csv_path, names=["timestamp", "status"])
    except:
        return pd.DataFrame()
        
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    return df

def load_nginx_data(metrics_dir):
    csv_path = os.path.join(metrics_dir, "nginx_access.log")
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(csv_path, names=["timestamp", "status"])
    except:
        return pd.DataFrame()
        
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    return df

def get_outage_window(metrics_dir):
    events_path = os.path.join(metrics_dir, "events.csv")
    if not os.path.exists(events_path):
        return None, None
    df = pd.read_csv(events_path)
    start = df[df["event"] == "DNS_BREAK"]["timestamp"].values
    end = df[df["event"] == "DNS_FIX"]["timestamp"].values
    
    t_start = pd.to_datetime(start[0], unit="s") if len(start) > 0 else None
    t_end = pd.to_datetime(end[0], unit="s") if len(end) > 0 else None
    return t_start, t_end

def plot_simple_ghost(metrics_dir):
    """Simplified Ghost Storm: Relative Time vs RPS"""
    df_cp = load_control_plane_data(metrics_dir)
    df_nginx = load_nginx_data(metrics_dir)
    df_dns = load_dns_data(metrics_dir)
    
    # Calculate Global Start Time (min of all timestamps)
    timestamps = []
    if not df_cp.empty: timestamps.append(df_cp["start_time"].min())
    if not df_nginx.empty: timestamps.append(df_nginx["timestamp"].min())
    if not df_dns.empty: timestamps.append(df_dns["timestamp"].min())
    
    if not timestamps:
        print("No data to plot")
        return
        
    global_start = min(timestamps)
    
    # Helper to process RPS and relative time
    def process_rps(df, time_col):
        if df.empty: return pd.Series()
        df = df.set_index(time_col)
        rps = df.resample("1s").size()
        # Convert index to relative seconds
        relative_index = (rps.index - global_start).total_seconds()
        return pd.Series(rps.values, index=relative_index)

    nginx_rps = process_rps(df_nginx, "timestamp")
    elb_rps = process_rps(df_cp, "start_time")
    dns_rps = process_rps(df_dns, "timestamp")
    
    # Plot
    plt.figure(figsize=(12, 6))
    
    if not nginx_rps.empty:
        plt.plot(nginx_rps.index, nginx_rps, label="LB Incoming RPS", color="green", linewidth=2)
    if not elb_rps.empty:
        plt.plot(elb_rps.index, elb_rps, label="Control Plane Incoming RPS", color="orange", linewidth=2, linestyle="--")
    if not dns_rps.empty:
        plt.plot(dns_rps.index, dns_rps, label="DNS Server Incoming RPS", color="purple", linewidth=2, linestyle=":")
    
    plt.ylabel("Requests / sec")
    plt.xlabel("Time (seconds)")
    plt.title("Traffic Flow: LB vs Control Plane vs DNS")
    plt.legend(loc="upper left")
    plt.grid(True)
    
    # Highlight Outage
    t_start, t_end = get_outage_window(metrics_dir)
    if t_start and t_end:
        rel_start = (t_start - global_start).total_seconds()
        rel_end = (t_end - global_start).total_seconds()
        plt.axvspan(rel_start, rel_end, color="pink", alpha=0.3, label="DNS Outage")

    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/plot_simple_ghost.png")
    plt.close()

def main():
    metrics_dir = "metrics"
    print("Generating Traffic Flow Plot...")
    plot_simple_ghost(metrics_dir)
    print(f"Done. Plot saved to {OUTPUT_DIR}/plot_simple_ghost.png")

if __name__ == "__main__":
    main()
