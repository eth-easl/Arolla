import requests
import time
import os
import threading
import random
import uuid

TARGET_URL = os.environ.get("TARGET_URL", "http://nlb/launch_instance")

# Configuration for different client profiles
PROFILES = {
    "good":  {"retries": 3,  "base": 0.1,  "max": 20.0, "jitter": "full", "timeout": 10},
    "bad":   {"retries": 10, "base": 0.01, "max": 1.0,  "jitter": "none", "timeout": 10},
    "sdk-a": {"retries": 2,  "base": 0.1,  "max": 20.0, "jitter": "full", "timeout": 10}, # Exp backoff
    "sdk-b": {"retries": 7,  "base": 0.2,  "max": 0.2,  "jitter": "none", "timeout": 10}, # Fixed 200ms
    "sdk-c": {"retries": 4,  "base": 0.05, "max": 0.05, "jitter": "none", "timeout": 2},  # Aggressive small timeout
    "sdk-d": {"retries": 0,  "base": 0,    "max": 0,    "jitter": "none", "timeout": 10}, # No retries
}

def get_env_count(name):
    return int(os.environ.get(f"NUM_{name.upper().replace('-', '_')}_CLIENTS", "0"))

def client_worker(worker_id, profile_name):
    print(f"Worker {worker_id} ({profile_name}) started")
    
    config = PROFILES.get(profile_name, PROFILES["good"])
    
    # Wait for system to stabilize
    time.sleep(10)
    
    # Stagger start
    time.sleep(random.uniform(0, 5))

    while True:
        request_id = str(uuid.uuid4())
        current_status_code = 0 
        attempts = 0
        
        # 1. Initial Request
        attempts += 1
        request_start_time = time.time()
        try:
            resp = requests.post(TARGET_URL, timeout=config["timeout"])
            current_status_code = resp.status_code
        except requests.exceptions.RequestException:
            current_status_code = 0

        # Log initial attempt
        latency = time.time() - request_start_time
        try:
            with open("/metrics/client.csv", "a") as f:
                # Format: timestamp, latency, status_code, client_type, request_id, attempt_number
                f.write(f"{time.time()},{latency},{current_status_code},{profile_name},{request_id},{attempts}\n")
        except Exception:
            pass

        # 2. Retry Logic
        if current_status_code == 503 or current_status_code == 0:
            retries = 0
            while retries < config["retries"]:
                # Calculate Delay
                if config["jitter"] == "full":
                    # Full Jitter: sleep = random_between(0, min(cap, base * 2^attempt))
                    temp = min(config["max"], config["base"] * (2 ** retries))
                    sleep_time = random.uniform(0, temp)
                else:
                    # No Jitter / Fixed
                    # If max == base, it's fixed delay. If not, it's linear or exponential without jitter
                    # For simplicity in this profile set:
                    # SDK-B (Fixed): base=0.2, max=0.2 -> always 0.2
                    # Bad (Linear-ish): base=0.01 -> let's just use base * (retries+1) capped at max
                    if config["max"] == config["base"]:
                        sleep_time = config["base"]
                    else:
                        sleep_time = min(config["base"] * (retries + 1), config["max"])
                
                time.sleep(sleep_time)
                
                # Retry Attempt
                attempts += 1
                retry_start = time.time()
                retry_status = 0
                try:
                    resp = requests.post(TARGET_URL, timeout=config["timeout"])
                    retry_status = resp.status_code
                except requests.exceptions.RequestException:
                    retry_status = 0
                
                # Log retry
                retry_latency = time.time() - retry_start
                try:
                    with open("/metrics/client.csv", "a") as f:
                        f.write(f"{time.time()},{retry_latency},{retry_status},{profile_name},{request_id},{attempts}\n")
                except Exception:
                    pass
                
                if retry_status == 200:
                    break
                
                retries += 1

        # Think time
        if profile_name in ["good", "sdk-a", "sdk-d"]:
            time.sleep(random.uniform(0.5, 2.0))
        else:
            # Aggressive clients are impatient
            time.sleep(random.uniform(0.1, 0.5))

threads = []

# Start clients for each profile
for profile in PROFILES.keys():
    count = get_env_count(profile)
    for i in range(count):
        t = threading.Thread(target=client_worker, args=(f"{profile}-{i}", profile))
        t.start()
        threads.append(t)

for t in threads:
    t.join()
