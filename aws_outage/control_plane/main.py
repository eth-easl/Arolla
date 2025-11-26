from fastapi import FastAPI, HTTPException
import boto3
import os
import time
from botocore.exceptions import EndpointConnectionError, ClientError

app = FastAPI()

# Configuration
DYNAMODB_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Initialize DynamoDB Client
# We create a new client per request or reuse? 
# Boto3 sessions are thread-safe, clients are generally thread-safe.
# However, to ensure we hit DNS every time (or respect TTL), we might need to be careful.
# Standard boto3 usage relies on urllib3 which pools connections. 
# If connection breaks, it should retry DNS resolution.

@app.post("/launch_instance")
async def launch_instance():
    status_code = 200
    try:
        # Simulate some logic that requires DynamoDB
        client = boto3.client(
            'dynamodb', 
            endpoint_url=DYNAMODB_ENDPOINT,
            region_name=REGION
        )
        
        client.list_tables()
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
        with open("/metrics/control_plane.csv", "a") as f:
            f.write(f"{time.time()},{status_code}\n")
