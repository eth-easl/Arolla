import requests
import time
import os
import threading
import random

TARGET_URL = os.environ.get("TARGET_URL", "http://nlb/launch_instance")
NUM_THREADS = 20

def client_worker(worker_id):
    print(f"Worker {worker_id} started")
    
    # AWS SDK retry config
    MAX_RETRIES = 3
    BASE_DELAY = 0.1  # 100ms
    MAX_DELAY = 20.0  # 20s
    
    while True:
        start_time = time.time()
        status_code = 0
        retry_count = 0
        
        while retry_count <= MAX_RETRIES:
            try:
                resp = requests.post(TARGET_URL, timeout=2)
                status_code = resp.status_code
                
                if resp.status_code == 200:
                    # Success: Break out of retry loop
                    break
                elif resp.status_code == 503:
                    # Service unavailable: Retry with exponential backoff
                    if retry_count < MAX_RETRIES:
                        # Calculate exponential backoff with jitter (AWS SDK style)
                        delay = min(BASE_DELAY * (2 ** retry_count), MAX_DELAY)
                        jitter = random.uniform(0, delay * 0.5)
                        sleep_time = delay + jitter
                        time.sleep(sleep_time)
                        retry_count += 1
                    else:
                        break
                else:
                    # Other errors: Don't retry
                    break
                    
            except requests.exceptions.RequestException as e:
                # Network/Connection error
                status_code = 0
                if retry_count < MAX_RETRIES:
                    delay = min(BASE_DELAY * (2 ** retry_count), MAX_DELAY)
                    jitter = random.uniform(0, delay * 0.5)
                    sleep_time = delay + jitter
                    time.sleep(sleep_time)
                    retry_count += 1
                else:
                    break
            
        latency = time.time() - start_time
        
        # Log metrics
        try:
            with open("/metrics/client.csv", "a") as f:
                f.write(f"{time.time()},{latency},{status_code}\n")
        except Exception:
            pass
        
        # Normal interval between requests if successful
        if status_code == 200:
            time.sleep(random.uniform(0.5, 2.0))

threads = []
for i in range(NUM_THREADS):
    t = threading.Thread(target=client_worker, args=(i,))
    t.start()
    threads.append(t)

for t in threads:
    t.join()
