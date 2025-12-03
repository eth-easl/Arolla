import pandas as pd
import matplotlib.pyplot as plt
import os
import glob
import numpy as np

# Set style
plt.style.use('ggplot')
plt.rcParams.update({'figure.max_open_warning': 0})

OUTPUT_DIR = "experiment_plots"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

def load_client_data(metrics_dir):
    """Loads client.csv from a metrics directory."""
    csv_path = os.path.join(metrics_dir, "client.csv")
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    
    try:
        # Try reading with new columns
        df = pd.read_csv(csv_path, names=["timestamp", "latency", "status_code", "client_type", "request_id", "attempt_number"])
    except:
        # Fallback
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

# --- GOAL 1 PLOTS ---

def plot_1_amplification(exp1_dirs):
    """Amplification vs Fraction of Bad Clients"""
    data = []
    
    for d in exp1_dirs:
        try:
            bad_count = int(d.split("_")[-1])
        except:
            continue
            
        fraction = bad_count / 20.0
        df = load_client_data(d)
        if df.empty: continue
        
        t_start, t_end = get_outage_window(d)
        if t_start and t_end:
            df = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)]
        
        total_attempts = len(df)
        unique_requests = df["request_id"].nunique()
        global_amp = total_attempts / unique_requests if unique_requests > 0 else 1
        
        amp_good = 1
        amp_bad = 1
        
        df_good = df[df["client_type"] == "good"]
        if not df_good.empty:
            amp_good = len(df_good) / df_good["request_id"].nunique()
            
        df_bad = df[df["client_type"] == "bad"]
        if not df_bad.empty:
            amp_bad = len(df_bad) / df_bad["request_id"].nunique()
            
        data.append({
            "Fraction Bad": fraction,
            "Global": global_amp,
            "Good Clients": amp_good,
            "Bad Clients": amp_bad
        })
        
    df_plot = pd.DataFrame(data).sort_values("Fraction Bad")
    
    plt.figure(figsize=(10, 6))
    plt.plot(df_plot["Fraction Bad"], df_plot["Global"], marker='o', label="Global", linewidth=3, color='black')
    plt.plot(df_plot["Fraction Bad"], df_plot["Good Clients"], marker='s', label="Good Clients", linestyle='--')
    plt.plot(df_plot["Fraction Bad"], df_plot["Bad Clients"], marker='^', label="Bad Clients", linestyle='--')
    
    plt.xlabel("Fraction of Aggressive Clients")
    plt.ylabel("Amplification Factor (Attempts / Request)")
    plt.title("Amplification vs Fraction of Bad Clients")
    plt.legend()
    plt.grid(True)
    plt.savefig(f"{OUTPUT_DIR}/plot_1_amplification.png")
    plt.close()

def plot_2_load_share(exp1_dirs):
    """Share of Backend Load vs Logical Traffic"""
    data = []
    
    for d in exp1_dirs:
        try:
            bad_count = int(d.split("_")[-1])
        except:
            continue
        
        fraction = bad_count / 20.0
        df = load_client_data(d)
        if df.empty: continue
        
        t_start, t_end = get_outage_window(d)
        if t_start and t_end:
            df = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)]
            
        logical_good = df[df["client_type"] == "good"]["request_id"].nunique()
        logical_bad = df[df["client_type"] == "bad"]["request_id"].nunique()
        total_logical = logical_good + logical_bad
        
        attempts_good = len(df[df["client_type"] == "good"])
        attempts_bad = len(df[df["client_type"] == "bad"])
        total_attempts = attempts_good + attempts_bad
        
        if total_logical > 0 and total_attempts > 0:
            data.append({
                "Fraction Bad": fraction,
                "% Logical (Bad)": (logical_bad / total_logical) * 100,
                "% Backend (Bad)": (attempts_bad / total_attempts) * 100
            })
            
    df_plot = pd.DataFrame(data).sort_values("Fraction Bad")
    
    plt.figure(figsize=(10, 6))
    width = 0.35
    x = np.arange(len(df_plot))
    plt.bar(x - width/2, df_plot["% Logical (Bad)"], width, label="% Logical (Bad)")
    plt.bar(x + width/2, df_plot["% Backend (Bad)"], width, label="% Backend (Bad)")
    
    plt.xticks(x, [f"{f:.2f}" for f in df_plot["Fraction Bad"]])
    plt.xlabel("Fraction of Aggressive Clients")
    plt.ylabel("Percentage (%)")
    plt.title("Share of Traffic: Bad Clients")
    plt.legend()
    plt.savefig(f"{OUTPUT_DIR}/plot_2_load_share.png")
    plt.close()

def plot_3_fairness(exp1_dirs):
    """Success Rate per Client Type"""
    data = []
    
    for d in exp1_dirs:
        try:
            bad_count = int(d.split("_")[-1])
        except:
            continue
        fraction = bad_count / 20.0
        
        df = load_client_data(d)
        if df.empty: continue
        
        good_success = len(df[(df["client_type"] == "good") & (df["status_code"] == 200)])
        bad_success = len(df[(df["client_type"] == "bad") & (df["status_code"] == 200)])
        
        good_clients = 20 - bad_count
        bad_clients = bad_count
        
        good_per_client = good_success / good_clients if good_clients > 0 else 0
        bad_per_client = bad_success / bad_clients if bad_clients > 0 else 0
        
        data.append({
            "Fraction Bad": fraction,
            "Good Clients": good_per_client,
            "Bad Clients": bad_per_client
        })
        
    df_plot = pd.DataFrame(data).sort_values("Fraction Bad")
    
    plt.figure(figsize=(10, 6))
    plt.plot(df_plot["Fraction Bad"], df_plot["Good Clients"], marker='o', label="Good Clients")
    plt.plot(df_plot["Fraction Bad"], df_plot["Bad Clients"], marker='x', label="Bad Clients")
    
    plt.xlabel("Fraction of Aggressive Clients")
    plt.ylabel("Successes per Client")
    plt.title("Fairness: Successes per Client vs Bad %")
    plt.legend()
    plt.savefig(f"{OUTPUT_DIR}/plot_3_fairness.png")
    plt.close()

def plot_4_tail_latency(exp1_dirs):
    """CDF of Latency"""
    target_dir = None
    for d in exp1_dirs:
        if "bad_4" in d:
            target_dir = d
            break
    
    if not target_dir: return
    
    df = load_client_data(target_dir)
    if df.empty: return
    
    t_start, t_end = get_outage_window(target_dir)
    if t_start and t_end:
        df = df[(df["timestamp"] >= t_start)]
        
    plt.figure(figsize=(10, 6))
    
    for ctype in ["good", "bad"]:
        subset = df[df["client_type"] == ctype]["latency"]
        if not subset.empty:
            sorted_data = np.sort(subset)
            yvals = np.arange(len(sorted_data)) / float(len(sorted_data) - 1)
            plt.plot(sorted_data, yvals, label=ctype)
            
    plt.xscale("log")
    plt.xlabel("Latency (s) - Log Scale")
    plt.ylabel("CDF")
    plt.title("Tail Latency CDF (20% Bad Clients)")
    plt.legend()
    plt.savefig(f"{OUTPUT_DIR}/plot_4_tail_latency.png")
    plt.close()

def plot_5_load_timeseries(exp1_dirs):
    """Stacked Area of Backend Load"""
    target_dir = None
    for d in exp1_dirs:
        if "bad_4" in d:
            target_dir = d
            break
    if not target_dir: return
    
    df = load_client_data(target_dir)
    if df.empty: return
    
    df = df.set_index("timestamp")
    df_resampled = df.groupby("client_type").resample("1s").size().unstack(level=0, fill_value=0)
    
    plt.figure(figsize=(10, 6))
    plt.stackplot(df_resampled.index, 
                  df_resampled.get("good", 0), 
                  df_resampled.get("bad", 0),
                  labels=["Good", "Bad"], colors=["green", "red"], alpha=0.6)
    
    t_start, t_end = get_outage_window(target_dir)
    if t_start and t_end:
        plt.axvspan(t_start, t_end, color="gray", alpha=0.2, label="Outage")
        
    plt.title("Backend Load Time-Series (20% Bad Clients)")
    plt.ylabel("Attempts / sec")
    plt.legend(loc="upper left")
    plt.savefig(f"{OUTPUT_DIR}/plot_5_load_timeseries.png")
    plt.close()

# --- GOAL 2 PLOTS ---

def plot_6_histogram(mix_dir):
    """Histogram of attempts per request by SDK"""
    df = load_client_data(mix_dir)
    if df.empty: return
    
    max_attempts = df.groupby(["client_type", "request_id"])["attempt_number"].max().reset_index()
    
    plt.figure(figsize=(10, 6))
    
    # Manual histogram since no seaborn
    client_types = max_attempts["client_type"].unique()
    for ctype in client_types:
        subset = max_attempts[max_attempts["client_type"] == ctype]["attempt_number"]
        plt.hist(subset, alpha=0.5, label=ctype, bins=range(1, 15))
        
    plt.title("Histogram of Attempts per Request by SDK")
    plt.xlabel("Attempts")
    plt.ylabel("Count")
    plt.legend()
    plt.savefig(f"{OUTPUT_DIR}/plot_6_histogram.png")
    plt.close()

def plot_7_waves(mix_dir):
    """Time-series of retry waves"""
    df = load_client_data(mix_dir)
    if df.empty: return
    
    t_start, t_end = get_outage_window(mix_dir)
    if t_start:
        zoom_end = t_start + pd.Timedelta(seconds=5)
        df = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= zoom_end)]
        
    df = df.set_index("timestamp")
    df_resampled = df.groupby("client_type").resample("100ms").size().unstack(level=0, fill_value=0)
    
    plt.figure(figsize=(12, 6))
    for col in df_resampled.columns:
        plt.plot(df_resampled.index, df_resampled[col], label=col)
        
    plt.title("Retry Waves (First 5s of Outage)")
    plt.ylabel("Attempts / 100ms")
    plt.legend()
    plt.savefig(f"{OUTPUT_DIR}/plot_7_waves.png")
    plt.close()

def plot_8_heatmap(mix_dir):
    """Heatmap: Attempt Index vs Time"""
    df = load_client_data(mix_dir)
    if df.empty: return
    
    t_start, t_end = get_outage_window(mix_dir)
    if t_start and t_end:
        df = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)]
        df["rel_time"] = (df["timestamp"] - t_start).dt.total_seconds()
    else:
        df["rel_time"] = (df["timestamp"] - df["timestamp"].min()).dt.total_seconds()
        
    df["time_bin"] = df["rel_time"].astype(int)
    
    heatmap_data = df.groupby(["attempt_number", "time_bin"]).size().unstack(fill_value=0)
    
    plt.figure(figsize=(12, 8))
    plt.imshow(heatmap_data, aspect='auto', cmap='viridis', origin='lower',
               extent=[heatmap_data.columns.min(), heatmap_data.columns.max(), 
                       heatmap_data.index.min(), heatmap_data.index.max()])
    plt.colorbar(label="Count")
    plt.xlabel("Time (s) since outage start")
    plt.ylabel("Attempt Number")
    plt.title("Heatmap: Attempt Index vs Time")
    plt.savefig(f"{OUTPUT_DIR}/plot_8_heatmap.png")
    plt.close()

def plot_9_amplification_dist(mix_dir):
    """Distribution of Amplification Factor per SDK"""
    df = load_client_data(mix_dir)
    if df.empty: return
    
    amp = df.groupby(["client_type", "request_id"])["attempt_number"].max().reset_index()
    
    plt.figure(figsize=(10, 6))
    
    # Manual boxplot
    data_to_plot = []
    labels = []
    for ctype in amp["client_type"].unique():
        data_to_plot.append(amp[amp["client_type"] == ctype]["attempt_number"].values)
        labels.append(ctype)
        
    plt.boxplot(data_to_plot, labels=labels)
    plt.title("Amplification Factor Distribution per SDK")
    plt.ylabel("Attempts per Request")
    plt.savefig(f"{OUTPUT_DIR}/plot_9_amplification_dist.png")
    plt.close()

def plot_10_variance(mix_dir, homo_dirs):
    """Total Retries Variance"""
    scenarios = {"Heterogeneous": mix_dir}
    scenarios.update(homo_dirs)
    
    data = []
    
    for name, d in scenarios.items():
        df = load_client_data(d)
        if df.empty: continue
        
        t_start, t_end = get_outage_window(d)
        if t_start and t_end:
            df = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)]
            
        df = df.set_index("timestamp")
        rps = df.resample("1s").size()
        
        std_dev = rps.std()
        data.append({"Scenario": name, "Std Dev": std_dev})
        
    df_plot = pd.DataFrame(data)
    
    plt.figure(figsize=(10, 6))
    plt.bar(df_plot["Scenario"], df_plot["Std Dev"])
    plt.title("Load Variance (Standard Deviation of RPS)")
    plt.ylabel("Standard Deviation")
    plt.savefig(f"{OUTPUT_DIR}/plot_10_variance.png")
    plt.close()

def main():
    # Find directories
    exp1_dirs = sorted(glob.glob("experiment_logs/metrics_exp1_bad_*"))
    exp2_mix = "experiment_logs/metrics_exp2_sdk_mix"
    
    homo_dirs = {}
    if os.path.exists("experiment_logs/metrics_exp1_bad_0"):
        homo_dirs["Homo Good"] = "experiment_logs/metrics_exp1_bad_0"
    if os.path.exists("experiment_logs/metrics_exp2_sdk_b_only"):
        homo_dirs["Homo SDK-B"] = "experiment_logs/metrics_exp2_sdk_b_only"
        
    print("Generating Goal 1 Plots...")
    plot_1_amplification(exp1_dirs)
    plot_2_load_share(exp1_dirs)
    plot_3_fairness(exp1_dirs)
    plot_4_tail_latency(exp1_dirs)
    plot_5_load_timeseries(exp1_dirs)
    
    print("Generating Goal 2 Plots...")
    if os.path.exists(exp2_mix):
        plot_6_histogram(exp2_mix)
        plot_7_waves(exp2_mix)
        plot_8_heatmap(exp2_mix)
        plot_9_amplification_dist(exp2_mix)
        plot_10_variance(exp2_mix, homo_dirs)
        
    print(f"Done. Plots saved to {OUTPUT_DIR}/")

if __name__ == "__main__":
    main()
