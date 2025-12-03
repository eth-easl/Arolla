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
        print("Phase: Normal Operation (60s)")
        time.sleep(60)
        
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
        print("Phase: Outage / Retry Storm (30s)")
        time.sleep(30)
        
        # 6. Recovery
        print("Phase: Recovering DNS...")
        log_event("DNS_FIX")
        try:
            requests.post("http://localhost:8080/fix_dns")
        except Exception as e:
            print(f"Error fixing DNS: {e}")
            
        print("Phase: Recovery (60s)")
        time.sleep(60)
        
        log_event("SIMULATION_END")
        
    finally:
        # 7. Cleanup
        print("Stopping Docker...")
        run_command("docker-compose down")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run AWS outage simulation")
    parser.add_argument("--good-clients", type=int, default=20, help="Number of standard (good) clients")
    parser.add_argument("--bad-clients", type=int, default=0, help="Number of aggressive (bad) clients")
    parser.add_argument("--sdk-a-clients", type=int, default=0, help="Number of SDK-A clients")
    parser.add_argument("--sdk-b-clients", type=int, default=0, help="Number of SDK-B clients")
    parser.add_argument("--sdk-c-clients", type=int, default=0, help="Number of SDK-C clients")
    parser.add_argument("--sdk-d-clients", type=int, default=0, help="Number of SDK-D clients")
    parser.add_argument("--ghost-clients", type=int, default=0, help="Number of Ghost clients")
    
    args = parser.parse_args()
    
    # Set environment variables for docker-compose
    os.environ["NUM_GOOD_CLIENTS"] = str(args.good_clients)
    os.environ["NUM_BAD_CLIENTS"] = str(args.bad_clients)
    os.environ["NUM_SDK_A_CLIENTS"] = str(args.sdk_a_clients)
    os.environ["NUM_SDK_B_CLIENTS"] = str(args.sdk_b_clients)
    os.environ["NUM_SDK_C_CLIENTS"] = str(args.sdk_c_clients)
    os.environ["NUM_SDK_D_CLIENTS"] = str(args.sdk_d_clients)
    os.environ["NUM_GHOST_CLIENTS"] = str(args.ghost_clients)
    
    total_clients = (args.good_clients + args.bad_clients + 
                     args.sdk_a_clients + args.sdk_b_clients + 
                     args.sdk_c_clients + args.sdk_d_clients +
                     args.ghost_clients)
    
    print(f"Running simulation with {total_clients} total clients")
    main()
