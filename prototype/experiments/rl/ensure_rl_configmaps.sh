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
VERSION=""

usage() {
  cat <<EOF
Usage: $(basename "$0") <version>

Apply RL controller ConfigMap(s) for rl_configs/<version>/ into ${NAMESPACE}.

  <version>   Directory under rl_configs/ (e.g. v5, v1). Applies every
              rb-rl-*.yaml in that directory (v1 ships v1-a and v1-b).

Examples:
  $(basename "$0") v5
  $(basename "$0") v1
EOF
}

while (( $# > 0 )); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -*) echo "ensure_rl_configmaps: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -n "${VERSION}" ]]; then
        echo "ensure_rl_configmaps: extra argument: $1" >&2
        usage >&2
        exit 2
      fi
      VERSION="$1"
      shift
      ;;
  esac
done

[[ -n "${VERSION}" ]] || {
  echo "ensure_rl_configmaps: missing <version>" >&2
  usage >&2
  exit 2
}

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

# Apply rl_configs/<version>/:
#   * one model zip per directory (model-<version>.zip or any *.zip)
#   * one or more rb-rl-*.yaml configs (e.g. v1-a / v1-b share model-v1.zip)
#   * ConfigMap name: rl-controller-<suffix> where suffix is the yaml stem
#     after stripping the rb-rl- prefix (rb-rl-v2.yaml → rl-controller-v2).
dir="${RL_ROOT}/${VERSION}/"
[[ -d "${dir}" ]] || {
  echo "ensure_rl_configmaps: unknown version ${VERSION} (${dir} not found)" >&2
  exit 1
}

zip=""
if [[ -f "${dir}model-${VERSION}.zip" ]]; then
  zip="${dir}model-${VERSION}.zip"
else
  shopt -s nullglob
  zip_files=("${dir}"*.zip)
  shopt -u nullglob
  if [[ -f "${zip_files[0]:-}" ]]; then
    zip="${zip_files[0]}"
    if ((${#zip_files[@]} > 1)); then
      echo "ensure_rl_configmaps: ${VERSION}: multiple zips, using $(basename "${zip}")" >&2
    fi
  fi
fi

shopt -s nullglob
yaml_files=("${dir}"rb-rl-*.yaml)
shopt -u nullglob

if ((${#yaml_files[@]} == 0)); then
  echo "ensure_rl_configmaps: no rb-rl-*.yaml under ${dir}" >&2
  exit 1
fi
if [[ -z "${zip}" ]]; then
  echo "ensure_rl_configmaps: missing model zip in ${dir}" >&2
  exit 1
fi

for yaml in "${yaml_files[@]}"; do
  stem="$(basename "${yaml}" .yaml)"
  suffix="${stem#rb-rl-}"
  apply_cm "rl-controller-${suffix}" "${zip}" "${yaml}"
done
