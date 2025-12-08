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
    print(f"Running local: {cmd}")
    subprocess.run(cmd, shell=True, check=True)

def run_remote_command(host, cmd):
    print(f"Running remote on {host}: {cmd}")
    # Assume shared filesystem, so we can cd to the same path
    cwd = os.getcwd()
    full_cmd = f"ssh {host} 'cd {cwd} && {cmd}'"
    subprocess.run(full_cmd, shell=True, check=True)

def main(args):
    backend_host = args.backend_host
    backend_api_url = f"http://{backend_host}:8080"
    
    # 1. Cleanup Metrics
    # Since filesystem is shared, this cleans it for both
    if os.path.exists(METRICS_DIR):
        shutil.rmtree(METRICS_DIR)
    os.makedirs(METRICS_DIR)
    
    # Initialize events file
    with open(EVENTS_FILE, "w") as f:
        f.write("timestamp,event\n")

    print(f"Starting Distributed Simulation Scenario...")
    print(f"Backend Node: {backend_host}")
    print(f"Client Node: Local")
    
    try:
        # 2. Start Docker
        print("Cleaning up old containers...")
        # Cleanup remote backend
        try:
            run_remote_command(backend_host, "sudo docker compose -f docker-compose-backend.yml down")
        except:
            pass
            
        # Cleanup local client
        try:
            run_command("sudo docker compose -f docker-compose-client.yml down")
        except:
            pass
            
        # Ensure metrics dir exists and has permissions on remote
        run_remote_command(backend_host, "mkdir -p metrics && chmod 777 metrics")

        print("Starting Backend on Remote Node...")
        run_remote_command(backend_host, "sudo docker compose -f docker-compose-backend.yml up --build -d")
        
        print("Starting Client on Local Node...")
        # Set TARGET_URL for client to point to backend NLB
        os.environ["TARGET_URL"] = f"http://{backend_host}/launch_instance"
        run_command("sudo docker compose -f docker-compose-client.yml up --build -d")
        
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
            requests.post(f"{backend_api_url}/break_dns")
            # Force restart control plane to ensure new connections fail immediately
            run_remote_command(backend_host, "sudo docker compose -f docker-compose-backend.yml restart control-plane")
        except Exception as e:
            print(f"Error triggering outage: {e}")
            
        # 5. Outage / Retry Storm
        print("Phase: Outage / Retry Storm (30s)")
        time.sleep(30)
        
        # 6. Recovery
        print("Phase: Recovering DNS...")
        log_event("DNS_FIX")
        try:
            requests.post(f"{backend_api_url}/fix_dns")
        except Exception as e:
            print(f"Error fixing DNS: {e}")
            
        print("Phase: Recovery (60s)")
        time.sleep(60)
        
        log_event("SIMULATION_END")
        
    finally:
        # Capture logs before cleanup
        print("Capturing debug logs...")
        try:
            run_remote_command(backend_host, "sudo docker compose -f docker-compose-backend.yml logs dns-server > metrics/dns_debug.log")
            run_remote_command(backend_host, "sudo docker compose -f docker-compose-backend.yml logs control-plane > metrics/cp_debug.log")
        except:
            print("Failed to capture logs")

        # 7. Cleanup
        print("Stopping Docker...")
        try:
            run_remote_command(backend_host, "sudo docker compose -f docker-compose-backend.yml down")
        except:
            pass
        try:
            run_command("sudo docker compose -f docker-compose-client.yml down")
        except:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run AWS outage simulation (Distributed)")
    parser.add_argument("--good-clients", type=int, default=20, help="Number of standard (good) clients")
    parser.add_argument("--bad-clients", type=int, default=0, help="Number of aggressive (bad) clients")
    parser.add_argument("--sdk-a-clients", type=int, default=0, help="Number of SDK-A clients")
    parser.add_argument("--sdk-b-clients", type=int, default=0, help="Number of SDK-B clients")
    parser.add_argument("--sdk-c-clients", type=int, default=0, help="Number of SDK-C clients")
    parser.add_argument("--sdk-d-clients", type=int, default=0, help="Number of SDK-D clients")
    parser.add_argument("--ghost-clients", type=int, default=0, help="Number of Ghost clients")
    parser.add_argument("--backend-host", type=str, default="pc735.emulab.net", help="Hostname/IP of the backend node")
    
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
    main(args)
