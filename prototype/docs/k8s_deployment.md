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

# 2. Deploy K8s cluster (also copies kubeconfig locally)
./deploy-k8s.sh

# 3. Install Istio + Gateway API (experimental CRDs for retry budgets)
./deploy-istio.sh

# 4. Deploy an app
./deploy-app.sh demo                 # Canary routing demo
./deploy-app.sh demo --test

./deploy-app.sh retry-budget         # Retry budget demo (GEP-3388)
./deploy-app.sh retry-budget --test

# List all available apps
./deploy-app.sh --list
```

## Files

| File | Purpose |
|------|---------|
| `k8s-config.sh` | All configurable variables (nodes, versions, CNI, Istio, etc.) |
| `deploy-k8s.sh` | **Layer 1** — Kubernetes cluster (kubeadm, containerd, CNI) |
| `deploy-istio.sh` | **Layer 2** — Istio service mesh + Gateway API CRDs (experimental) |
| `deploy-app.sh` | **Layer 3** — Unified app deployer (any app under `manifests/`) |
| `manifests/<app>/` | Per-app Kubernetes YAML manifests + config |
| `k8s-deploy-logs/` | Per-node logs (created at runtime) |

The scripts correspond to how production teams operate:

- **Platform team** runs `deploy-k8s.sh` + `deploy-istio.sh` once to set up the cluster + mesh
- **App teams** manage their own `manifests/<app>/` directories and deploy via `deploy-app.sh <app>`

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
| — | Copy kubeconfig to local machine | Local |

```bash
./deploy-k8s.sh              # Full deploy
./deploy-k8s.sh --step 4     # Run only step 4 on all nodes
./deploy-k8s.sh --master     # Deploy master only
./deploy-k8s.sh --workers    # Deploy workers only
./deploy-k8s.sh --join       # Print the kubeadm join command
./deploy-k8s.sh --verify     # Check cluster status
./deploy-k8s.sh --cleanup    # Tear down cluster & remove all packages
```

After deployment, `kubectl` and `istioctl` work from your local machine (kubeconfig is
automatically copied from the master node to `~/.kube/config`).

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
| 1 | Install `istioctl` CLI on master node + locally |
| 2 | Install Kubernetes Gateway API CRDs (v1.3.0, experimental channel) |
| 3 | Install Istio control plane with `PILOT_ENABLE_ALPHA_GATEWAY_API=true` |
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

The **experimental channel** is a superset of the standard channel — all standard CRDs
(`Gateway`, `HTTPRoute`, `GatewayClass`) are included, plus experimental ones like
`XBackendTrafficPolicy` for retry budgets (GEP-3388).

### Configuration

```bash
ISTIO_VERSION="1.27.5"         # 1.27+ required for Gateway API v1.3 experimental conformance
GATEWAY_API_VERSION="v1.3.0"
ISTIO_PROFILE="default"
```

### Key Istio flags

- `PILOT_ENABLE_ALPHA_GATEWAY_API=true` — enables experimental Gateway API features
  (`XBackendTrafficPolicy`, `BackendTLSPolicy`, etc.)
- `meshConfig.accessLogFile=/dev/stdout` — enables Envoy access logs for debugging

---

## Layer 3: App Deployment (`deploy-app.sh`)

Unified script that deploys any app from `manifests/<app>/`. Each app directory contains:

| File | Required | Purpose |
|------|----------|---------|
| `app.conf` | Yes | App metadata: `APP_NAME`, `APP_NS`, `APP_GATEWAY_NAME`, `APP_HOST`, `APP_DEPLOYMENTS` |
| `test.sh` | No | Custom test logic — defines `run_app_tests()` function |
| `*.yaml` | Yes | Kubernetes manifests (applied in lexicographic order) |

```bash
./deploy-app.sh <app>              # Deploy all manifests
./deploy-app.sh <app> --test       # Run traffic tests from local machine
./deploy-app.sh <app> --status     # Show resource status
./deploy-app.sh <app> --cleanup    # Remove all resources
./deploy-app.sh <app> --help       # Show app-specific help
./deploy-app.sh --list             # List all available apps
```

### Available Apps

#### `demo` — Canary Routing

Follows the [DevOpsCube tutorial](https://devopscube.com/istio-ingress-kubernetes-gateway-api/).

```
manifests/demo/
├── app.conf                  # APP_NS=istio-test, APP_HOST=test.example.com
├── test.sh                   # 10 requests showing canary split
├── namespace.yaml            # istio-test namespace (sidecar injection)
├── backend-v1.yaml           # Deployment (3 replicas) + Service
├── backend-v2.yaml           # Deployment (2 replicas) + Service
├── gateway.yaml              # Gateway (HTTP/80, istio class, NodePort)
├── httproute.yaml            # HTTPRoute (50/50 canary traffic split)
├── destination-rules.yaml    # DestinationRules (circuit breaking)
└── client.yaml               # Client config (gateway host)
```

```
External Client
  │
  ▼
Gateway (istio-test)              ← Envoy proxy pod, NodePort
  │
  ▼
HTTPRoute (50/50 canary split)    ← Routing rules (host, path, weights)
  │              │
  ▼              ▼
backend-v1    backend-v2          ← App pods with Istio sidecars
(3 replicas)  (2 replicas)
  │              │
  ▼              ▼
DestinationRule  DestinationRule   ← Circuit breaking (Istio-native)
```

#### `retry-budget` — Retry Budgets (GEP-3388)

Implements [GEP-3388: Retry Budgets](https://gateway-api.sigs.k8s.io/geps/gep-3388/) using
the Gateway API `XBackendTrafficPolicy` resource. Istio 1.27+ natively translates this into
Envoy's `circuit_breakers.retry_budget` configuration — no extra controllers needed.

```
manifests/retry-budget/
├── app.conf                       # APP_NS=retry-budget-test, APP_HOST=retry.example.com
├── test.sh                        # Traffic test with Envoy stats + config verification
├── namespace.yaml                 # retry-budget-test namespace
├── backend.yaml                   # Single backend, tunable FAILURE_RATE (0-100%)
├── gateway.yaml                   # Gateway (HTTP/80, NodePort)
├── httproute.yaml                 # HTTPRoute (retry 503, max 3 attempts, 100ms backoff)
├── backend-traffic-policy.yaml    # XBackendTrafficPolicy (retry budget 20%, min 10)
└── client.yaml                    # Client config
```

```
Client → Gateway → HTTPRoute (retry 503, max 3, 100ms backoff)
                        │
                        ▼
                   Service "backend"  (FAILURE_RATE=30%)
                        ↑
              XBackendTrafficPolicy
              budget: 20%, min_concurrency: 10
                        │
                        │ (Istio 1.27 translates natively)
                        ▼
              Envoy circuit_breakers.retry_budget
```

**How it works:**
- Backend randomly returns 503 based on FAILURE_RATE (tunable, default 30%)
- The HTTPRoute says "retry 503 up to 3 times" — so the Gateway retries failed requests
- The `XBackendTrafficPolicy` says "cap retries at 20% of active requests"
- Istio 1.27 translates this into Envoy's `circuit_breakers.retry_budget` automatically
- Under low load: retries succeed (budget not exhausted), client sees 200
- Under high load: budget exhausted, additional retries are rejected with 503

**What Istio generates in Envoy:**

| Gateway API resource | Envoy config |
|---|---|
| `HTTPRoute.retry.codes: [503]` | `retryPolicy.retriableStatusCodes: [503]` |
| `HTTPRoute.retry.attempts: 3` | `retryPolicy.numRetries: 3` |
| `HTTPRoute.retry.backoff: 100ms` | `retryPolicy.retryBackOff.baseInterval: 0.100s` |
| `XBackendTrafficPolicy.retryConstraint.budget.percent: 20` | `circuitBreakers.thresholds.retryBudget.budgetPercent: 20` |
| `XBackendTrafficPolicy.retryConstraint.minRetryRate.count: 10` | `circuitBreakers.thresholds.retryBudget.minRetryConcurrency: 10` |

**Key difference: HTTPRoute retry vs RetryConstraint:**

| | HTTPRoute retry | XBackendTrafficPolicy retryConstraint |
|---|---|---|
| **Scope** | Per-request, per-route | Per-service, global across all clients |
| **Question** | "Should this request be retried?" | "Can the system afford this retry?" |
| **Config** | status codes, max attempts, backoff | budget %, time window, min rate |
| **Prevents** | Individual request failures | Retry storms / cascading failures |

### Testing

Tests run from your **local machine** by curling through the Gateway's NodePort:

```bash
./deploy-app.sh demo --test            # Canary split (10 requests)
./deploy-app.sh retry-budget --test    # Send requests, show Envoy stats + access logs
```

Manual testing:

```bash
# Get the NodePort (printed by --test and --status)
NODE_PORT=$(kubectl -n <APP_NS> get svc \
    -l gateway.networking.k8s.io/gateway-name=<GATEWAY_NAME> \
    -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}')

# Curl through the Gateway
curl -s -H 'Host: <APP_HOST>' http://<MASTER_HOST>:$NODE_PORT/
```

---

## Deploying Your Own App

Create a new directory `manifests/<your-app>/` with:

1. **`app.conf`** (required):

```bash
APP_NAME="My App"
APP_NS="my-app"
APP_GATEWAY_NAME="my-gateway"
APP_HOST="my-app.example.com"
APP_DEPLOYMENTS="my-service"    # space-separated deployment names
```

2. **`namespace.yaml`** with sidecar injection:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: my-app
  labels:
    istio-injection: enabled
```

3. **Deployments + Services** for your app.

4. **`gateway.yaml`**:

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

5. **`httproute.yaml`**:

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

6. (Optional) **`test.sh`** for custom test logic:

```bash
# Sourced by deploy-app.sh. Has access to: gateway_url, APP_HOST, APP_NS, etc.
run_app_tests() {
    local gateway_url="$1"
    # Your custom test logic here
    curl -s -H "Host: ${APP_HOST}" "${gateway_url}/"
}
```

Then deploy:

```bash
./deploy-app.sh my-app
./deploy-app.sh my-app --test
```

---

## Cleanup

Tear down in reverse layer order:

```bash
# 1. Remove apps
./deploy-app.sh retry-budget --cleanup
./deploy-app.sh demo --cleanup

# 2. Remove Istio + Gateway API
./deploy-istio.sh --cleanup

# 3. Remove entire cluster
./deploy-k8s.sh --cleanup
```
