import requests
import time
import os
import threading
import random

TARGET_URL = os.environ.get("TARGET_URL", "http://nlb/launch_instance")
NUM_THREADS = int(os.environ.get("NUM_CLIENTS", "20"))

def client_worker(worker_id):
    print(f"Worker {worker_id} started")
    
    # AWS SDK retry config
    MAX_RETRIES = 3
    BASE_DELAY = 0.1  # 100ms
    MAX_DELAY = 20.0  # 20s
    
    # Wait for system to stabilize
    print(f"Worker {worker_id}: Waiting 10s for system stabilization...")
    time.sleep(10)
    
    # Stagger start to prevent thundering herd
    time.sleep(random.uniform(0, 5))

    while True:
        # --- Request Loop ---
        current_status_code = 0 # To track the final status of this request cycle
        
        # 1. Initial Request
        request_start_time = time.time()
        try:
            resp = requests.post(TARGET_URL, timeout=10)
            current_status_code = resp.status_code
        except requests.exceptions.RequestException:
            current_status_code = 0  # Connection Error

        # Log the initial attempt
        latency = time.time() - request_start_time
        try:
            with open("/metrics/client.csv", "a") as f:
                f.write(f"{time.time()},{latency},{current_status_code}\n")
        except Exception:
            pass

        # 2. Retry Logic (only if transient failure)
        if current_status_code == 503 or current_status_code == 0:
            retries = 0
            while retries < MAX_RETRIES:
                # Exponential Backoff with Jitter
                delay = min(BASE_DELAY * (2 ** retries), MAX_DELAY)
                jitter = random.uniform(0, delay * 0.5)
                sleep_time = delay + jitter
                time.sleep(sleep_time)
                
                # Retry Attempt
                retry_start = time.time()
                retry_status = 0
                try:
                    resp = requests.post(TARGET_URL, timeout=10)
                    retry_status = resp.status_code
                except requests.exceptions.RequestException:
                    retry_status = 0
                
                # Log the retry
                retry_latency = time.time() - retry_start
                try:
                    with open("/metrics/client.csv", "a") as f:
                        f.write(f"{time.time()},{retry_latency},{retry_status}\n")
                except Exception:
                    pass
                
                if retry_status == 200:
                    break # Success! Exit retry loop
                
                retries += 1

        # Sleep before next new request (simulate user think time)
        time.sleep(random.uniform(0.5, 2.0))
        


threads = []
for i in range(NUM_THREADS):
    t = threading.Thread(target=client_worker, args=(i,))
    t.start()
    threads.append(t)

for t in threads:
    t.join()
