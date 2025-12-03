import subprocess
import os
import shutil
import time

def run_command(cmd):
    print(f"Running: {cmd}")
    subprocess.run(cmd, shell=True, check=True)

def save_metrics(dest_dir):
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    shutil.copytree("metrics", dest_dir)
    print(f"Saved metrics to {dest_dir}")

def run_experiment_1_bad_sweep():
    print("\n=== Starting Experiment 1: Bad Client Sweep ===")
    # Total 20 clients. Vary Bad clients: 0, 1, 2, 4, 10 (0%, 5%, 10%, 20%, 50%)
    scenarios = [
        {"good": 20, "bad": 0},
        {"good": 19, "bad": 1},
        {"good": 18, "bad": 2},
        {"good": 16, "bad": 4},
        {"good": 10, "bad": 10},
    ]

    for s in scenarios:
        good = s["good"]
        bad = s["bad"]
        print(f"\n--- Running Scenario: Good={good}, Bad={bad} ---")
        
        # Run simulation
        cmd = f"python3 run_scenario.py --good-clients {good} --bad-clients {bad}"
        run_command(cmd)
        
        # Save metrics
        dest = f"experiment_logs/metrics_exp1_bad_{bad}"
        save_metrics(dest)

def run_experiment_2_sdk_mix():
    print("\n=== Starting Experiment 2: SDK Mix ===")
    # Mix of all SDKs: 5 of each (Total 20)
    print("\n--- Running Scenario: Mixed SDKs ---")
    
    cmd = "python3 run_scenario.py --sdk-a-clients 5 --sdk-b-clients 5 --sdk-c-clients 5 --sdk-d-clients 5"
    run_command(cmd)
    
    dest = "experiment_logs/metrics_exp2_sdk_mix"
    save_metrics(dest)

    print("\n--- Running Scenario: Homogeneous SDK-B ---")
    cmd = "python3 run_scenario.py --sdk-b-clients 20"
    run_command(cmd)
    dest = "experiment_logs/metrics_exp2_sdk_b_only"
    save_metrics(dest)

def main():
    # Ensure clean start
    if os.path.exists("metrics"):
        shutil.rmtree("metrics")
    if os.path.exists("experiment_logs"):
        shutil.rmtree("experiment_logs")
    os.makedirs("experiment_logs")
    
    run_experiment_1_bad_sweep()
    run_experiment_2_sdk_mix()
    
    print("\nAll experiments completed.")

if __name__ == "__main__":
    main()
