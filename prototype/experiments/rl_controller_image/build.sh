#!/usr/bin/env bash
# Build the rl-controller container image for in-cluster runs.
#
# Usage:  prototype/experiments/rl_controller_image/build.sh [TAG]
#
# Default TAG = v3 (matches model-v3.zip / rb-rl-v3.yaml).  The Job
# manifest references `rl-controller:${TAG}` with `imagePullPolicy: Never`
# so the tag must match what gets imported via distribute.sh.
#
# rl_controller.py lives one level up; we COPY it via a build-context path
# relative to this directory, so the docker build context is the image
# directory itself plus the parent script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TAG="${1:-v3}"
IMAGE="rl-controller:${TAG}"

# Stage rl_controller.py into the build context so the Dockerfile's
# `COPY rl_controller.py` resolves locally. We delete the staged copy on
# exit so the source-of-truth stays in the experiments directory.
trap 'rm -f "${SCRIPT_DIR}/rl_controller.py"' EXIT
cp "${EXP_DIR}/rl_controller.py" "${SCRIPT_DIR}/rl_controller.py"

echo "[build] docker build -t ${IMAGE} ${SCRIPT_DIR} (platform=linux/amd64)"
# `--platform=linux/amd64` and `--load` make this work on arm64 laptops
# (Apple Silicon) where the image otherwise gets built for arm64 and
# refuses to start on the x86_64 Emulab workers.
docker buildx build \
  --platform=linux/amd64 \
  --load \
  -t "${IMAGE}" \
  "${SCRIPT_DIR}"

# Save + gzip the image so distribute.sh can ship it to each worker over
# scp. ~1 GB raw → ~400 MB gzipped (mostly the torch wheel).
OUT="${SCRIPT_DIR}/rl-controller-${TAG}.tar.gz"
echo "[build] saving image to ${OUT}"
docker save "${IMAGE}" | gzip > "${OUT}"

echo "[build] done: ${IMAGE} → ${OUT} ($(du -h "${OUT}" | cut -f1))"
