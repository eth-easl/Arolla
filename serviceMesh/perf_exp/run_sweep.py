import subprocess
import time
import json
import re
import sys
import os
import shutil
from datetime import datetime

# Configuration
START_RPS = 500
STEP_RPS = 500
MAX_RPS = 10000
DURATION = 20  # Duration per level

# Setup Result Directory
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
SWEEP_DIR = f"results/sweep_{timestamp}"
SUMMARY_FILE = f"{SWEEP_DIR}/summary.json"
CLIENT_APP_LABEL = "app=client"

if not os.path.exists(SWEEP_DIR):
    os.makedirs(SWEEP_DIR)

def run_cmd(cmd, check=True):
    """Run shell command and return output"""
    try:
        result = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
        return result.decode('utf-8').strip()
    except subprocess.CalledProcessError as e:
        if check:
            print(f"Error running cmd: {cmd}\nOutput: {e.output.decode('utf-8')}")
        return None

def get_client_pod():
    """Get the name of the running client pod"""
    return run_cmd(f"kubectl get pod -l {CLIENT_APP_LABEL} --field-selector=status.phase=Running -o jsonpath='{{.items[0].metadata.name}}'")

def run_experiment(rps, client_pod):
    print(f"\n[Orchestrator] Starting run for RPS={rps}...")
    
    # Exec command
    cmd = f"kubectl exec -it {client_pod} -c client -- ./load-generator {rps} {DURATION} http://service-a"
    
    print(f"[Orchestrator] Running generator on {client_pod}...")
    output = run_cmd(cmd, check=False)
    
    if not output:
        print("[Orchestrator] Generator failed or timed out.")
        return None

    # Parse Output for Summary
    results = {"rps": rps, "duration": DURATION}
    params = ["Mean", "P50", "P90", "P99", "Requests", "SuccessRate"]
    
    for line in output.split('\n'):
        line = line.strip()
        for param in params:
            if line.startswith(f"{param}:"):
                try:
                    val_str = line.split(":")[1].strip().replace(" ms", "").replace("%", "")
                    val = float(val_str)
                    results[param.lower()] = val
                except ValueError:
                    pass
    
    print(f"[Orchestrator] Stats: {results}")

    # Download CSV
    local_csv = f"{SWEEP_DIR}/rps_{rps}.csv"
    print(f"[Orchestrator] Downloading logs to {local_csv}...")
    cp_cmd = f"kubectl cp {client_pod}:requests.csv {local_csv} -c client"
    run_cmd(cp_cmd)

    return results

def main():
    print(f"=== Starting Detailed Sweep ===")
    print(f"Results Directory: {SWEEP_DIR}")
    print(f"Range: {START_RPS} to {MAX_RPS}, Step: {STEP_RPS}")

    client_pod = get_client_pod()
    if not client_pod:
        print("Error: Client pod not found")
        sys.exit(1)

    all_results = []
    
    current_rps = START_RPS
    while current_rps <= MAX_RPS:
        res = run_experiment(current_rps, client_pod)
        if res:
            all_results.append(res)
            
            # Stop condition (example: < 90% success)
            if res.get("successrate", 100.0) < 90.0:
                print(f"!!! Breaking Point Reached at {current_rps} RPS !!!")
                break
        
        current_rps += STEP_RPS
        time.sleep(5) # Cooldown
    
    # Save Summary
    with open(SUMMARY_FILE, 'w') as f:
        json.dump(all_results, f, indent=2)
    
    print(f"\n=== Sweep Complete ===")
    print(f"Summary: {SUMMARY_FILE}")
    print(f"CSVs: {SWEEP_DIR}/rps_*.csv")

if __name__ == "__main__":
    main()
