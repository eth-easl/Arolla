#!/usr/bin/env bash
# Distribute rl-controller-${TAG}.tar.gz to every worker via scp + ctr.
#
# Usage:  prototype/experiments/rl_controller_image/distribute.sh [TAG]
#
# Default TAG = v3.  Reads worker hostnames from prototype/k8s-config.sh.
# Fails fast on the first worker that fails — partial distribution would
# leave the pod stuck `ErrImageNeverPull` on whichever worker the kube
# scheduler picks.
#
# Idempotent: ctr image import overwrites an existing tag, and the local
# tarball is reusable across calls (build.sh refreshes it).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=../../k8s-config.sh
source "${PROTO_DIR}/k8s-config.sh"

TAG="${1:-v3}"
IMAGE="rl-controller:${TAG}"
TARBALL="${SCRIPT_DIR}/rl-controller-${TAG}.tar.gz"

[[ -f "${TARBALL}" ]] || {
  echo "tarball not found: ${TARBALL}" >&2
  echo "run build.sh ${TAG} first." >&2
  exit 1
}

key_opt=""
[[ -n "${SSH_KEY}" ]] && key_opt="-i ${SSH_KEY}"

for h in "${WORKER_HOSTS[@]}"; do
  echo "[distribute] ${h}: copying $(basename "${TARBALL}")"
  # shellcheck disable=SC2086
  scp ${SSH_OPTS} ${key_opt} "${TARBALL}" "${SSH_USER}@${h}:/tmp/"
  echo "[distribute] ${h}: ctr -n=k8s.io image import"
  # shellcheck disable=SC2086
  ssh ${SSH_OPTS} ${key_opt} "${SSH_USER}@${h}" \
    "sudo ctr -n=k8s.io image import /tmp/$(basename "${TARBALL}")"
  echo "[distribute] ${h}: ${IMAGE} ready"
done

echo "[distribute] done. Image ${IMAGE} is loaded on ${#WORKER_HOSTS[@]} workers."
