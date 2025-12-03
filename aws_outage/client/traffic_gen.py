import asyncio
import aiohttp
import time
import os
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
    "ghost": {"retries": 3, "base": 1.0,  "max": 10.0, "jitter": "full", "timeout": 1.5},  # Machine gun retries
}

def get_env_count(name):
    return int(os.environ.get(f"NUM_{name.upper().replace('-', '_')}_CLIENTS", "0"))

async def log_metric(latency, status_code, profile_name, request_id, attempt):
    try:
        with open("/metrics/client.csv", "a") as f:
            # Format: timestamp, latency, status_code, client_type, request_id, attempt_number
            f.write(f"{time.time()},{latency},{status_code},{profile_name},{request_id},{attempt}\n")
    except Exception:
        pass

async def client_worker(session, profile_name):
    config = PROFILES.get(profile_name, PROFILES["good"])
    
    # Wait for system to stabilize
    await asyncio.sleep(10)
    
    # Stagger start
    await asyncio.sleep(random.uniform(0, 5))

    while True:
        request_id = str(uuid.uuid4())
        current_status_code = 0 
        attempts = 0
        
        # 1. Initial Request
        attempts += 1
        headers = {
            "X-Request-ID": request_id,
            "X-Attempt-Number": str(attempts),
            "X-Client-Type": profile_name
        }
        
        request_start_time = time.time()
        try:
            async with session.post(TARGET_URL, headers=headers, timeout=config["timeout"]) as resp:
                current_status_code = resp.status
                await resp.read() # Ensure we read the body
        except Exception:
            current_status_code = 0

        # Log initial attempt
        latency = time.time() - request_start_time
        await log_metric(latency, current_status_code, profile_name, request_id, attempts)

        # 2. Retry Logic
        if current_status_code == 503 or current_status_code == 502 or current_status_code == 504 or current_status_code == 0:
            retries = 0
            while retries < config["retries"]:
                # Calculate Delay
                if config["jitter"] == "full":
                    temp = min(config["max"], config["base"] * (2 ** retries))
                    sleep_time = random.uniform(0, temp)
                else:
                    if config["max"] == config["base"]:
                        sleep_time = config["base"]
                    else:
                        sleep_time = min(config["base"] * (retries + 1), config["max"])
                
                await asyncio.sleep(sleep_time)
                
                # Retry Attempt
                attempts += 1
                retry_start = time.time()
                retry_status = 0
                headers = {
                    "X-Request-ID": request_id,
                    "X-Attempt-Number": str(attempts),
                    "X-Client-Type": profile_name
                }
                try:
                    async with session.post(TARGET_URL, headers=headers, timeout=config["timeout"]) as resp:
                        retry_status = resp.status
                        await resp.read()
                except Exception:
                    retry_status = 0
                
                # Log retry
                retry_latency = time.time() - retry_start
                await log_metric(retry_latency, retry_status, profile_name, request_id, attempts)
                
                if retry_status == 200:
                    break
                
                retries += 1

        # Think time
        if profile_name in ["good", "sdk-a", "sdk-d"]:
            await asyncio.sleep(random.uniform(0.5, 2.0))
        else:
            # Aggressive clients are impatient
            await asyncio.sleep(random.uniform(0.1, 0.5))

async def main():
    # Increase limit for high concurrency
    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for profile in PROFILES.keys():
            count = get_env_count(profile)
            for i in range(count):
                tasks.append(asyncio.create_task(client_worker(session, profile)))
        
        if tasks:
            await asyncio.gather(*tasks)
        else:
            print("No clients configured. Exiting.")

if __name__ == "__main__":
    asyncio.run(main())
