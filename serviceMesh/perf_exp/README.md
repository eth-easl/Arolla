# Performance Experiment (perf_exp)

This directory contains the setup for a performance experiment measuring latency and retries across a call chain:
`Client -> Service A -> Service B`

## Architecture
- **Client Node**: `pc729.emulab.net` (hostname `node0.retry.latencymodel.emulab.net`)
- **Service Node**: `pc735.emulab.net` (hostname `node1.retry.latencymodel.emulab.net`)
- **Network**: Kubernetes v1.29 (Kubeadm + Flannel) + Istio Service Mesh

## Prerequisites
- Two specific Emulab nodes: `pc729` and `pc735`.
- SSH access to these nodes (key-based).
- Code located at `/proj/latencymodel/yazhuoz/globalRetryBudget/serviceMesh/perf_exp` (Accessible via NFS on all nodes).

## 1. Cluster Setup
**WARNING**: Running this script will **RESET** (wipe) the cluster on the target nodes.

```bash
bash setup_cluster.sh
```
This script:
1. Resets Kubernetes on both nodes.
2. Installs Docker, Containerd, and Kubeadm.
3. Initializes the Master on `pc729`.
4. Joins `pc735` as a worker.
5. Configures your local `~/.kube/config` to access the cluster.

## 2. Install Istio
After the cluster is ready, install Istio (if not already done):

```bash
# 1. Install Istio on Master
ssh -o StrictHostKeyChecking=no yazhuoz@node0.retry.latencymodel.emulab.net "curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.22.1 TARGET_ARCH=x86_64 sh - && cd istio-1.22.1 && sudo ./bin/istioctl install --set profile=demo -y"

# 2. Enable Sidecar Injection for default namespace
kubectl label namespace default istio-injection=enabled --overwrite
```

## 3. Deployment

### Step 3.1: Label Nodes
Pin the client and services to specific machines:
```bash
kubectl label node node0.retry.latencymodel.emulab.net app-tier=client --overwrite
kubectl label node node1.retry.latencymodel.emulab.net app-tier=service --overwrite
```

### Step 3.2: Build and Import Images
Since we are using a local registry logic (images present on nodes), we must build the image and import it into `containerd` on **BOTH** nodes.

```bash
# Node 0 (Client Node)
ssh -o StrictHostKeyChecking=no yazhuoz@node0.retry.latencymodel.emulab.net "cd /proj/latencymodel/yazhuoz/globalRetryBudget/serviceMesh/perf_exp && sudo docker build -t service-mesh-demo:latest . && sudo docker save service-mesh-demo:latest | sudo ctr -n k8s.io images import -"

# Node 1 (Service Node)
ssh -o StrictHostKeyChecking=no yazhuoz@node1.retry.latencymodel.emulab.net "cd /proj/latencymodel/yazhuoz/globalRetryBudget/serviceMesh/perf_exp && sudo docker build -t service-mesh-demo:latest . && sudo docker save service-mesh-demo:latest | sudo ctr -n k8s.io images import -"
```

### Step 3.3: Apply Manifests
```bash
kubectl apply -f k8s_deployment.yaml
kubectl apply -f istio_config.yaml
```

## 4. Verification

### Check Pods
Ensure pods are running and distributed: `client` on `node0`, `services` on `node1`.
```bash
kubectl get pods -o wide
```

### Verify Connectivity
Run a request from the client pod:
```bash
kubectl exec -it $(kubectl get pod -l app=client -o jsonpath="{.items[0].metadata.name}") -c client -- python -c "import requests; print(requests.get('http://service-a').text)"
```
**Expected Output:**
```
Service A -> Service B Success
```
