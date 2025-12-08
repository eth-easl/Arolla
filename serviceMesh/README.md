# Service Mesh Retry Interception Experiment

## Goal
The goal of this experiment is to demonstrate and verify that **Istio (Envoy)** can intercept and log upstream retries.

Specifically, we want to see that when `Service A` calls `Service B` and `Service B` fails, the **cluster subsystems** (specifically Istio's sidecar proxy on `Service A`) handle the retry logic (as configured in `VirtualService`) and our custom `EnvoyFilter` (Lua script) detects these retries. This confirms that the behavior is driven by the infrastructure, not the application code.

## Components
- **Service A**: A Flask app that calls Service B.
- **Service B**: A Flask app that fails 50% of the time (returns 500) to trigger retries.
- **Client**: A script that sends traffic to Service A.
- **Istio Config**:
    - `VirtualService`: Configures retry policy for Service B (3 attempts, retry on 5xx).
    - `EnvoyFilter`: Injects a Lua script into the `SIDECAR_OUTBOUND` chain to log retry attempts.

## Prerequisites
- Kubernetes cluster (Kind, Minikube, etc.)
- `kubectl` installed
- `docker` installed
- Istio installed on the cluster

## Deployment Steps

### 1. Build and Load Docker Image
Since we are using a local image, we need to build it and load it into the cluster (e.g., for Kind).

```bash
# Build the image (using DOCKER_BUILDKIT=0 if you have permission issues)
DOCKER_BUILDKIT=0 docker build -t service-mesh-demo:latest serviceMesh/

# Load into Kind (replace 'simple' with your cluster name if different)
kind load docker-image service-mesh-demo:latest --name simple
```

### 2. Deploy Services
Deploy Service A and Service B to the cluster.

```bash
kubectl apply -f serviceMesh/k8s_deployment.yaml
```

### 3. Apply Istio Configuration
Apply the VirtualService and EnvoyFilter.

```bash
kubectl apply -f serviceMesh/istio_config.yaml
```

## Verification Steps

### 1. Run the Client
Run the client script inside the cluster to generate traffic. This uses the same image we built earlier.

```bash
kubectl run client -it --rm --image=service-mesh-demo:latest --image-pull-policy=Never --restart=Never -- python client.py
```
You should see the client printing responses. Some might be 500s (when retries are exhausted), but many should be 200s.

### 2. Check Envoy Logs
Inspect the sidecar logs of `Service A` to see the Lua script output.

```bash
# Get the pod name for Service A
POD_NAME=$(kubectl get pod -l app=service-a -o jsonpath="{.items[0].metadata.name}")

# Grep for the log message defined in the EnvoyFilter
kubectl logs $POD_NAME -c istio-proxy | grep "Lua upstream filter"

# or
kubectl logs $POD_NAME -c istio-proxy --follow
```

### Expected Output
You should see logs indicating the attempt count for requests:
```
[info][lua] ... script log: Lua upstream filter: envoy_on_request, attempt=1
[info][lua] ... script log: Lua upstream filter: envoy_on_request, attempt=2
```
This confirms that the Envoy proxy is intercepting the retries initiated by the VirtualService configuration.
