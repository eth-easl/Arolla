# Kubernetes Cluster Deployment

Automated deployment of a Kubernetes cluster with Istio service mesh and Gateway API on remote Ubuntu 22.04 nodes. Runs entirely from your local machine over SSH.

## Prerequisites

- Ubuntu 22.04 on all remote nodes
- SSH access from your local machine to every node (password-less recommended)
- `sudo` privileges on the remote nodes

## Quick Start

```bash
# 1. Edit config
vi k8s-config.sh

# 2. Deploy K8s cluster
./deploy-k8s.sh

# 3. Install Istio + Gateway API
./deploy-istio.sh

# 4. Deploy demo app
./deploy-demo.sh

# 5. Run traffic tests
./deploy-demo.sh --test
```

## Files

| File | Purpose |
|------|---------|
| `k8s-config.sh` | All configurable variables (nodes, versions, CNI, Istio, etc.) |
| `deploy-k8s.sh` | **Layer 1** — Kubernetes cluster (kubeadm, containerd, CNI) |
| `deploy-istio.sh` | **Layer 2** — Istio service mesh + Gateway API CRDs |
| `deploy-demo.sh` | **Layer 3** — Demo application (manifests, testing, cleanup) |
| `manifests/demo/` | Kubernetes YAML manifests for the demo app |
| `k8s-deploy-logs/` | Per-node logs (created at runtime) |

The three scripts correspond to how production teams operate:

- **Platform team** runs `deploy-k8s.sh` + `deploy-istio.sh` once to set up the cluster + mesh
- **App teams** manage their own manifests and deploy scripts (like `deploy-demo.sh`)

---

## Layer 1: Kubernetes Cluster (`deploy-k8s.sh`)

| Step | Description | Runs on |
|------|-------------|---------|
| 1 | Set hostnames | All nodes |
| 2 | Update `/etc/hosts` | All nodes |
| 3 | Disable swap & configure kernel | All nodes |
| 4 | Install containerd + crictl | All nodes |
| 5 | Install kubeadm, kubelet, kubectl | All nodes |
| 6 | Initialize cluster (`kubeadm init`) | Master |
| 7 | Join workers to cluster | Workers |
| 8 | Install CNI plugin (Calico/Flannel) | Master |
| 9 | Install Metrics Server | Master |
| 10 | Verify cluster | Master |

```bash
./deploy-k8s.sh              # Full deploy
./deploy-k8s.sh --step 4     # Run only step 4 on all nodes
./deploy-k8s.sh --master     # Deploy master only
./deploy-k8s.sh --workers    # Deploy workers only
./deploy-k8s.sh --join       # Print the kubeadm join command
./deploy-k8s.sh --verify     # Check cluster status
./deploy-k8s.sh --cleanup    # Tear down cluster & remove all packages
```

### Adding More Workers

```bash
WORKER_HOSTS=("pc751.emulab.net" "pc752.emulab.net")
WORKER_HOSTNAMES=("worker01" "worker02")
```

Then re-run `./deploy-k8s.sh`.

---

## Layer 2: Istio + Gateway API (`deploy-istio.sh`)

Reference: [Setting up Istio Ingress With Kubernetes Gateway API](https://devopscube.com/istio-ingress-kubernetes-gateway-api/)

| Step | Description |
|------|-------------|
| 1 | Install `istioctl` CLI on master node |
| 2 | Install Kubernetes Gateway API CRDs (v1.3.0) |
| 3 | Install Istio control plane (istiod) |
| 4 | Verify GatewayClass + installation |

```bash
./deploy-istio.sh              # Full install (steps 1-4)
./deploy-istio.sh --step 3     # Run only step 3
./deploy-istio.sh --status     # Show Istio & Gateway API status
./deploy-istio.sh --cleanup    # Remove Istio + Gateway API CRDs
```

Istio automatically creates two `GatewayClass` resources:
- **`istio`** — built-in controller that manages Gateway resources and provisions Envoy proxy pods
- **`istio-remote`** — for gateways managed by a remote cluster (multi-cluster mesh)

### Configuration

```bash
ISTIO_VERSION="1.24.2"
GATEWAY_API_VERSION="v1.3.0"
ISTIO_PROFILE="default"
```

---

## Layer 3: Demo Application (`deploy-demo.sh`)

The demo follows the [DevOpsCube tutorial](https://devopscube.com/istio-ingress-kubernetes-gateway-api/) and applies YAML manifests from `manifests/demo/`:

```
manifests/demo/
├── namespace.yaml            # istio-test namespace (sidecar injection enabled)
├── backend-v1.yaml           # Deployment (3 replicas) + Service
├── backend-v2.yaml           # Deployment (2 replicas) + Service
├── gateway.yaml              # Gateway (HTTP/80, istio class, NodePort)
├── httproute.yaml            # HTTPRoute (50/50 canary traffic split)
├── destination-rules.yaml    # DestinationRules (circuit breaking, etc.)
└── client.yaml               # Client config (gateway host, endpoints, split)
```

```bash
./deploy-demo.sh              # Deploy all manifests
./deploy-demo.sh --test       # Run traffic tests from local machine via Gateway NodePort
./deploy-demo.sh --status     # Show demo resource status
./deploy-demo.sh --cleanup    # Remove all demo resources
```

Edit the YAML files directly to change configuration. This is the standard
production pattern — each app team manages their own manifests directory.

### Architecture

```
External Client
  │
  ▼
Gateway (istio-test namespace)        ← Envoy proxy pod, auto-provisioned by Istio
  │                                      NodePort service on bare-metal
  ▼
HTTPRoute (50/50 canary split)        ← Routing rules (host, path, weights)
  │              │
  ▼              ▼
backend-v1    backend-v2              ← App pods with Istio sidecars
(3 replicas)  (2 replicas)
  │              │
  ▼              ▼
DestinationRule  DestinationRule       ← Circuit breaking, connection pool,
                                         outlier detection (Istio-native)
```

Key components:
- **GatewayClass**: Tells K8s which controller implements gateways (auto-created by Istio)
- **Gateway**: Gateway API resource — Istio provisions an Envoy proxy pod for it
- **HTTPRoute**: Routing rules (host, path, weighted backends for canary)
- **DestinationRule**: Istio-native traffic policies (circuit breaking, load balancing)
- **Sidecar proxy**: Envoy proxy auto-injected into application pods

### Testing

Tests run from your **local machine** by curling through the Gateway's NodePort:

```bash
# Automated: sends 10 requests and shows the canary split
./deploy-demo.sh --test
```

Manual testing:

```bash
# Get the NodePort (printed by --test and --status)
NODE_PORT=$(kubectl -n istio-test get svc -l gateway.networking.k8s.io/gateway-name=istio-gateway \
    -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}')

# Curl through the Gateway
curl -s -H 'Host: test.example.com' http://<MASTER_HOST>:$NODE_PORT/

# Verbose (see response headers, connection info)
curl -sv -H 'Host: test.example.com' http://<MASTER_HOST>:$NODE_PORT/
```

You should see roughly 50/50 responses between `hello from backend v1` and `hello from backend v2`.

---

## Deploying Your Own App

Use the demo as a template. Create your own `manifests/<app-name>/` directory:

1. **Namespace** with sidecar injection:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: my-app
  labels:
    istio-injection: enabled
```

2. **Deployments + Services** for your app.

3. **Gateway**:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: my-gateway
  namespace: my-app
  annotations:
    networking.istio.io/service-type: NodePort    # bare-metal; remove for cloud LB
spec:
  gatewayClassName: istio
  listeners:
    - name: http
      protocol: HTTP
      port: 80
      allowedRoutes:
        namespaces:
          from: Same
```

4. **HTTPRoute**:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: my-route
  namespace: my-app
spec:
  parentRefs:
    - name: my-gateway
  hostnames:
    - "my-app.example.com"
  rules:
    - backendRefs:
        - name: my-service
          port: 80
```

5. (Optional) **DestinationRule** for circuit breaking:

```yaml
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: my-service-dr
  namespace: my-app
spec:
  host: my-service.my-app.svc.cluster.local
  trafficPolicy:
    connectionPool:
      tcp: { maxConnections: 100 }
      http: { http1MaxPendingRequests: 50 }
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 30s
      baseEjectionTime: 30s
```

---

## Cleanup

Tear down in reverse layer order:

```bash
# 1. Remove demo app
./deploy-demo.sh --cleanup

# 2. Remove Istio + Gateway API
./deploy-istio.sh --cleanup

# 3. Remove entire cluster
./deploy-k8s.sh --cleanup
```
