#!/usr/bin/env bash
# Apply RL controller ConfigMaps (model.zip + config.yaml) into online-boutique.
# Idempotent: uses kubectl create --dry-run=client -o yaml | kubectl apply.
#
# Used by run_full_sweep.sh; safe to run manually after cluster bring-up.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=../k8s-config.sh
source "${PROTO_DIR}/k8s-config.sh"

NAMESPACE="online-boutique"
RL_ROOT="${SCRIPT_DIR}/rl_configs"

apply_cm() {
  local name="$1" zip="$2" yaml="$3"
  [[ -f "${zip}" ]] || {
    echo "ensure_rl_configmaps: missing model zip: ${zip}" >&2
    exit 1
  }
  [[ -f "${yaml}" ]] || {
    echo "ensure_rl_configmaps: missing config yaml: ${yaml}" >&2
    exit 1
  }
  kubectl -n "${NAMESPACE}" create configmap "${name}" \
    --from-file=model.zip="${zip}" \
    --from-file=config.yaml="${yaml}" \
    --dry-run=client -o yaml | kubectl apply -f -
  printf '[ensure_rl_configmaps] %s/%s\n' "${NAMESPACE}" "${name}" >&2
}

kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || {
  echo "ensure_rl_configmaps: namespace ${NAMESPACE} not found — check KUBECONFIG / k8s-config.sh" >&2
  exit 1
}

apply_cm rl-controller-v1-a "${RL_ROOT}/v1/model-v1.zip" "${RL_ROOT}/v1/rb-rl-v1-a.yaml"
apply_cm rl-controller-v1-b "${RL_ROOT}/v1/model-v1.zip" "${RL_ROOT}/v1/rb-rl-v1-b.yaml"
apply_cm rl-controller-v2 "${RL_ROOT}/v2/model-v2.zip" "${RL_ROOT}/v2/rb-rl-v2.yaml"
apply_cm rl-controller-v3 "${RL_ROOT}/v3/model-v3.zip" "${RL_ROOT}/v3/rb-rl-v3.yaml"
apply_cm rl-controller-v4 "${RL_ROOT}/v4/model-v4.zip" "${RL_ROOT}/v4/rb-rl-v4.yaml"
