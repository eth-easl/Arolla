# Post-mortem simulation - documentation

The incident can be reproduced with 5 nodes:

- One control plane node
- Two nodes for caches (memcached)
- One node for the database (MySQL)
- One node for mcrouter and a mock app

## 1. Creating the cluster

The following steps are different depending on whether the incident
is being reproduced locally or on a remote cluster. Follow the instructions
accordingly.

### 1a. Local development - creating the cluster

The cluster can be created using `kind` ([Kubernetes IN Docker](https://kind.sigs.k8s.io/)).
The configuration for the cluster can be found in `kind-config.yaml`.

Run this command:

```sh
kind create cluster --name slack-2022-02 --config kind-config.yaml
```

### 1b. Production - creating the cluster

[TBA]

## 2. Deploy the application

1. Label the nodes (these will be used by the `nodeSelector` policy
   in the Kubernetes deployments):

```sh
kubectl label nodes slack-2022-02-worker role=database
kubectl label nodes slack-2022-02-worker2 role=cache
kubectl label nodes slack-2022-02-worker3 role=cache
kubectl label nodes slack-2022-02-worker4 role=app_mcrouter
```

> [!NOTE]
> If the names of your nodes are different, update them accordingly.

1. Apply the Kubernetes config maps

```sh
kubectl apply -f configs.yaml
```

The configuration maps are especially useful for the database, as
they restrict the maximum number of connections to 20 (thus making the
reproduction of the incident easier):

```
    [mysqld]
    max_connections = 20
    innodb_buffer_pool_size = 128M
-
```

1. Run the deployments:

```sh
kubectl apply -f deployments.yaml
```

The `infrastructure.yaml` file contains one Kubernetes deployment and
one Kubernetes service for each of the following:

- MySQL database, restricted to max. 20 connections at a time
- `memcached` nodes (2 replicas, on different nodes)
- `mcrouter` which is a "load balancer" in front of the cache nodes
- A custom Flask app that mimics the Slack frontend servers and
  exposes an API (more on the app later)

To verify that everything works successfully, try requesting an API URL:

```sh
curl -i http://<APP_HOSTNAME>:8080/api/message/1
```

You should see a similar response:

```
HTTP/1.1 200 OK
Server: gunicorn
Connection: close
Content-Type: application/json
Content-Length: 44
X-Cache: MISS

{"content":"This is test message 1","id":1}
```

By requesting the same URL again, the `X-Cache` header will be different:

```
HTTP/1.1 200 OK
Server: gunicorn
Connection: close
Content-Type: application/json
Content-Length: 44
X-Cache: HIT

{"content":"This is test message 1","id":1}
```

> [!NOTE]
> If you are running your cluster locally, don't forget to expose
> the app by running `kubectl port-forward svc/slack-clone-app-svc 8080:8080`
> and requesting `http://localhost:8080/api/message/1`

## Simulate the incident

Now that we have our app up and running, how can we reproduce what happened
to Slack?

1. Generate some load using `hey`:
   `hey -c 5000 -z 1m http://<APP_HOSTNAME>:8080/api/message/42`

> [!NOTE]
> You can generate some load by adding a pre-made pod to the cluster:
> `kubectl apply -f load-generator.yaml`, which will run the command above.
