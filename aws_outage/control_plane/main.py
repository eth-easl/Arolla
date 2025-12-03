from fastapi import FastAPI, HTTPException, Request
import boto3
import os
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from botocore.exceptions import EndpointConnectionError, ClientError
from botocore.config import Config

app = FastAPI()

# Configuration
DYNAMODB_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "5"))  # Capacity limit

# Capacity control
worker_semaphore = asyncio.Semaphore(MAX_WORKERS)
current_queue_depth = 0
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

def call_dynamodb():
    """Synchronous function to call DynamoDB - raises exceptions for async handler to catch"""
    try:
        client = boto3.client(
            'dynamodb', 
            endpoint_url=DYNAMODB_ENDPOINT,
            region_name=REGION,
            config=Config(connect_timeout=0.1, read_timeout=0.1, retries={'max_attempts': 3})
        )
        client.list_tables()
    except Exception as e:
        # Re-raise so the executor can propagate it
        raise e

@app.post("/launch_instance")
async def launch_instance(request: Request):
    global current_queue_depth
    
    # Extract headers
    req_id = request.headers.get("X-Request-ID", "unknown")
    attempt = request.headers.get("X-Attempt-Number", "1")
    client_type = request.headers.get("X-Client-Type", "unknown")
    
    # Track queue depth (requests waiting for capacity)
    current_queue_depth += 1
    queue_depth_at_entry = current_queue_depth
    
    status_code = 200
    start_time = time.time()
    
    # Wait for available worker capacity
    async with worker_semaphore:
        current_queue_depth -= 1  # Got capacity, no longer queued
        
        try:
            # Simulate realistic CPU/IO work (reduced for fast demo)
            await asyncio.sleep(0.05)
            
            # Log DNS Query attempt (simulated by call_dynamodb)
            # We log this as a separate event if needed, or just rely on the main log
            # For now, let's just log the main request completion with details
            
            # Run synchronous boto3 call in thread pool
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(executor, call_dynamodb)
            
            return {"status": "success", "instance_id": f"i-{int(time.time())}"}
            
        except (EndpointConnectionError, ClientError) as e:
            status_code = 503
            print(f"AWS Error: {e}")
            raise HTTPException(status_code=503, detail="Service Unavailable: Dependency Failure")
        except Exception as e:
            status_code = 500
            print(f"Internal Error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Log metrics
            processing_time = time.time() - start_time
            with open("/metrics/control_plane.csv", "a") as f:
                # Format: timestamp, status_code, queue_depth, processing_time, req_id, attempt, client_type
                f.write(f"{time.time()},{status_code},{queue_depth_at_entry},{processing_time},{req_id},{attempt},{client_type}\n")
