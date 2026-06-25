# rl-controller-image — in-cluster controller

Builds a small container that runs `rl_controller.py` inside the cluster as
a `Job`. Combined with the loader's `/window` HTTP endpoint and the
in-cluster `kubernetes` client, the controller observes retry pressure and
patches the retry-budget DestinationRule at runtime.

## Layout

- `Dockerfile` — `python:3.11-slim` + sb3 + torch-cpu + the kubernetes
  client. `rl_controller.py` is copied last so dep layers cache.
- `build.sh [TAG]` — `docker build`, then `docker save | gzip` into
  `rl-controller-<TAG>.tar.gz`.
- `distribute.sh [TAG]` — `scp` the tarball to every worker listed in
  `prototype/k8s-config.sh` and `ctr -n=k8s.io image import` it.

The default tag is `v3`, matching `rl_configs/v3/model-v3.zip`. Bump the tag
whenever the model or controller code changes so old runs stay reproducible.

## One-shot per cluster lifecycle

```bash
# 1) Build + ship the image (rebuild only on rl_controller.py / model change)
prototype/experiments/rl_controller_image/build.sh
prototype/experiments/rl_controller_image/distribute.sh

# 2) RBAC + ConfigMap (one-shot per cluster)
kubectl apply -f prototype/manifests/online-boutique/rl-controller/rbac.yaml
kubectl -n online-boutique create configmap rl-controller-v3 \
  --from-file=model.zip=prototype/experiments/rl_configs/v3/model-v3.zip \
  --from-file=config.yaml=prototype/experiments/rl_configs/v3/rb-rl-v3.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
```

`run-experiment.sh --rl-controller-config` templates and applies the Job
manifest at the start of each policy run, follows its logs, copies
`/var/log/rl/` out, and deletes the Job.
