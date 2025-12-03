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

def load_client_data(metrics_dir):
    csv_path = os.path.join(metrics_dir, "client.csv")
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(csv_path, names=["timestamp", "latency", "status_code", "client_type", "request_id", "attempt_number"])
    except:
        return pd.DataFrame()
        
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    return df

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

def plot_simple_ghost(metrics_dir):
    """Simplified Ghost Storm: Time vs RPS"""
    df_client = load_client_data(metrics_dir)
    df_cp = load_control_plane_data(metrics_dir)
    df_nginx = load_nginx_data(metrics_dir)
    
    if df_client.empty:
        print("No client data for plot")
        return

    # Align timestamps
    t_start, t_end = get_outage_window(metrics_dir)
    
    # 1. Client RPS (Outgoing Attempts)
    df_client = df_client.set_index("timestamp")
    client_rps = df_client.resample("1s").size()
    
    # 2. ELB RPS (Control Plane Ingress)
    # Use start_time to capture when requests hit the ELB/CP
    if not df_cp.empty:
        df_cp = df_cp.set_index("start_time")
        elb_rps = df_cp.resample("1s").size()
    else:
        elb_rps = pd.Series()

    # 3. Nginx RPS (Incoming to LB)
    if not df_nginx.empty:
        df_nginx = df_nginx.set_index("timestamp")
        nginx_rps = df_nginx.resample("1s").size()
    else:
        nginx_rps = pd.Series()
    
    # Plot
    plt.figure(figsize=(12, 6))
    
    plt.plot(client_rps.index, client_rps, label="Client RPS (Outgoing)", color="blue", linewidth=2)
    if not nginx_rps.empty:
        plt.plot(nginx_rps.index, nginx_rps, label="Nginx RPS (Incoming to LB)", color="green", linewidth=2, linestyle="-.")
    if not elb_rps.empty:
        plt.plot(elb_rps.index, elb_rps, label="Control Plane RPS (Ingress)", color="orange", linewidth=2, linestyle="--")
    
    plt.ylabel("Requests / sec")
    plt.xlabel("Time")
    plt.title("Simplified Ghost Storm: Retries Persisting After Recovery")
    plt.legend(loc="upper left")
    plt.grid(True)
    
    # Highlight Outage
    if t_start and t_end:
        plt.axvspan(t_start, t_end, color="pink", alpha=0.3, label="DNS Outage")

    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/plot_simple_ghost.png")
    plt.close()

def main():
    metrics_dir = "metrics"
    print("Generating Simplified Ghost Storm Plot...")
    plot_simple_ghost(metrics_dir)
    print(f"Done. Plot saved to {OUTPUT_DIR}/plot_simple_ghost.png")

if __name__ == "__main__":
    main()
