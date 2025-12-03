import subprocess
import time
import requests
import os
import shutil
import argparse

METRICS_DIR = "metrics"
EVENTS_FILE = os.path.join(METRICS_DIR, "events.csv")

def log_event(name):
    timestamp = time.time()
    with open(EVENTS_FILE, "a") as f:
        f.write(f"{timestamp},{name}\n")
    print(f"[{time.strftime('%H:%M:%S')}] Event: {name}")

def run_command(cmd):
    subprocess.run(cmd, shell=True, check=True)

def main():
    # 1. Cleanup Metrics
    if os.path.exists(METRICS_DIR):
        shutil.rmtree(METRICS_DIR)
    os.makedirs(METRICS_DIR)
    
    # Initialize events file
    with open(EVENTS_FILE, "w") as f:
        f.write("timestamp,event\n")

    print("Starting Simulation Scenario...")
    
    try:
        # 2. Start Docker
        print("Cleaning up old containers...")
        subprocess.run("docker-compose down", shell=True, check=False) # Ignore errors if down fails
        run_command("docker-compose up --build -d")
        log_event("SIMULATION_START")
        
        # Wait for services to be ready
        print("Waiting for services to stabilize (10s)...")
        time.sleep(10)
        
        # 3. Normal Operation
        print("Phase: Normal Operation (10s)")
        time.sleep(10)
        
        # 4. Trigger Outage
        print("Phase: Triggering DNS Failure...")
        log_event("DNS_BREAK")
        try:
            requests.post("http://localhost:8080/break_dns")
            # Force restart control plane to ensure new connections fail immediately
            run_command("docker-compose restart control-plane")
        except Exception as e:
            print(f"Error triggering outage: {e}")
            
        # 5. Outage / Retry Storm
        print("Phase: Outage / Retry Storm (15s)")
        time.sleep(15)
        
        # 6. Recovery
        print("Phase: Recovering DNS...")
        log_event("DNS_FIX")
        try:
            requests.post("http://localhost:8080/fix_dns")
        except Exception as e:
            print(f"Error fixing DNS: {e}")
            
        print("Phase: Recovery (10s)")
        time.sleep(10)
        
        log_event("SIMULATION_END")
        
    finally:
        # 7. Cleanup
        print("Stopping Docker...")
        run_command("docker-compose down")

    # 8. Visualize
    print("Generating Visualization...")
    run_command("python3 visualize.py")
    print("Done. Check simulation_results.png")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run AWS outage simulation")
    parser.add_argument("-n", "--num-clients", type=int, default=20, 
                        help="Number of client threads to simulate (default: 20)")
    args = parser.parse_args()
    
    # Set environment variable for docker-compose
    os.environ["NUM_CLIENTS"] = str(args.num_clients)
    
    print(f"Running simulation with {args.num_clients} clients")
    main()
