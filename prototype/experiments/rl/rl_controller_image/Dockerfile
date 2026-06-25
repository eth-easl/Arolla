# Controller image — runs rl_controller.py inside the cluster.
#
# The python:3.11-slim base keeps the image under ~1 GB even with torch-cpu.
# Dependencies match what run_rl.sh installs into ~/.rl-env, so the same
# rl_controller.py source tree works locally and inside the pod.
#
# Two-stage isn't worth it here — the image is built once per
# rl_controller.py / model-vN change, distributed to every worker via
# `ctr image import`, and the build cache lives on the dev machine.

FROM python:3.11-slim

# Install OS deps that scientific Python wheels need on slim. ca-certificates
# is what the kubernetes client TLS layer needs; libgomp1 is required by
# stable-baselines3's MKL/OMP bindings on glibc.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Pin major versions; the controller has been validated against these.
# torch MUST be installed before stable-baselines3, otherwise pip resolves
# the SB3 dependency chain against the regular PyPI index and pulls in the
# 2-GB CUDA-enabled torch (plus nvidia-cudnn-cu13 etc.) — even with
# --extra-index-url passed to the *second* pip command. By installing the
# CPU torch wheel first we satisfy SB3's torch dep and skip the GPU tower.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.0,<3" \
 && pip install --no-cache-dir \
        "stable-baselines3>=2.0,<3" \
        "gymnasium>=1.0,<2" \
        "kubernetes>=29,<36" \
        "numpy>=2.0" \
        "pyyaml" \
        "requests"

# rl_controller.py + rl_obs_schema.py + rl_obs_envoy.py are the only
# python files we need. rl_obs_schema is the loader/controller contract
# (WindowBucket + histogram edges, used by the loader-buckets obs path);
# rl_obs_envoy is the Envoy /stats parser + 18-feature derivation used
# by the sidecar obs path. A stale tag of the controller image without
# either of them would crash on import. Copy all three last so
# dependency layers cache across builds.
COPY rl_controller.py /app/rl_controller.py
COPY rl_obs_schema.py /app/rl_obs_schema.py
COPY rl_obs_envoy.py /app/rl_obs_envoy.py
WORKDIR /app

# `--out-dir`, `--config`, etc. come from the Job manifest's `args:` list.
ENTRYPOINT ["python3", "/app/rl_controller.py"]
