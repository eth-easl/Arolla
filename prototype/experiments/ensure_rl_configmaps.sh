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
  # Optional VecNormalize statistics: ship them in the ConfigMap when
  # they live next to the model so the controller restores the same
  # observation distribution at inference. Older v1..v4 model bundles
  # don't have it — the controller logs a warning and runs raw.
  #
  # We accept multiple filename conventions (canonical
  # ``vecnormalize_stats.pkl``, labmate's per-model
  # ``<model-stem>-normalize.pkl``, or generic ``vecnormalize.pkl``)
  # but always mount under the canonical key so the in-cluster path
  # stays at /etc/rl/vecnormalize_stats.pkl.
  local model_dir="${zip%/*}"
  local model_stem
  model_stem="$(basename "${zip}" .zip)"
  local stats=""
  for candidate in \
      "${model_dir}/vecnormalize_stats.pkl" \
      "${model_dir}/vecnormalize.pkl" \
      "${model_dir}/${model_stem}-normalize.pkl"; do
    if [[ -f "${candidate}" ]]; then
      stats="${candidate}"
      break
    fi
  done
  local extra_args=()
  if [[ -n "${stats}" ]]; then
    extra_args+=("--from-file=vecnormalize_stats.pkl=${stats}")
  fi
  # `${arr[@]+"${arr[@]}"}` keeps `set -u` happy when the array is empty
  # (v1..v4 bundles ship without vecnormalize_stats.pkl).
  kubectl -n "${NAMESPACE}" create configmap "${name}" \
    --from-file=model.zip="${zip}" \
    --from-file=config.yaml="${yaml}" \
    ${extra_args[@]+"${extra_args[@]}"} \
    --dry-run=client -o yaml | kubectl apply -f -
  if (( ${#extra_args[@]} > 0 )); then
    printf '[ensure_rl_configmaps] %s/%s (with %s)\n' \
      "${NAMESPACE}" "${name}" "$(basename "${stats}")" >&2
  else
    printf '[ensure_rl_configmaps] %s/%s\n' "${NAMESPACE}" "${name}" >&2
  fi
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

# Future versions (v5+) auto-register once the labmate drops the
# trained model and a matching `rb-rl-<dir>.yaml` config under
# rl_configs/<dir>/. The ConfigMap name follows the same `rl-controller-<dir>`
# convention as the explicit entries above. No action needed for the
# v1..v4 set: that's already covered above.
for dir in "${RL_ROOT}"/v*/; do
  [[ -d "${dir}" ]] || continue
  name="$(basename "${dir}")"
  case "${name}" in
    v1|v2|v3|v4) continue ;;  # already applied above
  esac
  yaml="${dir}rb-rl-${name}.yaml"
  zip_files=("${dir}"*.zip)
  if [[ ! -f "${yaml}" ]]; then
    continue
  fi
  if [[ ! -f "${zip_files[0]}" ]]; then
    echo "ensure_rl_configmaps: skipping ${name} (no model zip yet)" >&2
    continue
  fi
  apply_cm "rl-controller-${name}" "${zip_files[0]}" "${yaml}"
done
