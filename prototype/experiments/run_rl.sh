#!/usr/bin/env bash
# Run the rb-rl-v1 shadow controller over the scenarios embedded in the
# chosen rl_configs/*/rb-rl-*.yaml (see the `scenarios:` block in that file).
# Run the rb-rl-v1 shadow controller over the scenarios embedded in the
# chosen rl_configs/*/rb-rl-*.yaml (see the `scenarios:` block in that file).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

CONFIG_PATH="${SCRIPT_DIR}/rl/rl_configs/v1/rb-rl-v1-a.yaml"
SCENARIO_SET="core"
SCENARIO_IDS=""
OUTPUT_ROOT=""
DRY_RUN=false
SKIP_PLOTS=false
SHADOW_MODE=false
NUM_LOADERS_OVERRIDE=""

RL_IMAGE_TAG=""
RL_CONFIGMAP=""
RL_LOADER_PORT_BASE=""
RL_LOADER_HOST=""
RL_OBS_MODE=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --config <yaml>        RL config path (default: ${CONFIG_PATH})
  --set <core|smoke>     Scenario set from config (default: ${SCENARIO_SET})
  --ids <S01,S12>        Explicit scenario IDs from the core set
  --output-root <dir>    Output root; default comes from config + timestamp
  --num-loaders <n>      Override experiment.num_loaders
  --shadow               Run the controller in shadow mode (no DestinationRule patches)
  --skip-plots           Skip final RL-vs-default comparison plotting
  --rl-image-tag <tag>   Image tag for the in-cluster controller Job (default: v5)
  --rl-configmap <name>  ConfigMap name (default: rl-controller-v5)
  --rl-loader-port-base <n>  /window port base on the loader (default: 8765)
  --rl-loader-host <ip>  Address pods use to reach the loader (default: \$CLIENT_IP)
  --obs-mode <mode>      Observation transport. auto|envoy|buckets|rows
                         (default: auto = envoy → buckets → rows cascade).
  -n, --dry-run          Print run commands  without executing
  -h, --help             Show this message
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --set) SCENARIO_SET="$2"; shift 2 ;;
    --ids) SCENARIO_IDS="$2"; shift 2 ;;
    --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
    --num-loaders) NUM_LOADERS_OVERRIDE="$2"; shift 2 ;;
    --shadow) SHADOW_MODE=true; shift ;;
    --skip-plots) SKIP_PLOTS=true; shift ;;
    --rl-image-tag) RL_IMAGE_TAG="$2"; shift 2 ;;
    --rl-configmap) RL_CONFIGMAP="$2"; shift 2 ;;
    --rl-loader-port-base) RL_LOADER_PORT_BASE="$2"; shift 2 ;;
    --rl-loader-host) RL_LOADER_HOST="$2"; shift 2 ;;
    --obs-mode) RL_OBS_MODE="$2"; shift 2 ;;
    -n|--dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -f "${CONFIG_PATH}" ]] || CONFIG_PATH="${SCRIPT_DIR}/${CONFIG_PATH}"
[[ -f "${CONFIG_PATH}" ]] || { echo "config not found: ${CONFIG_PATH}" >&2; exit 1; }

# ---------------------------------------------------------------------------
# RL virtualenv — isolates numpy 2.x + SB3 from system packages compiled
# against numpy 1.x (matplotlib, pandas) that break on numpy 2.
# ---------------------------------------------------------------------------
RL_VENV="${HOME}/.rl-env"
if [[ ! -d "${RL_VENV}" ]]; then
  echo "[rl-v1] creating RL virtualenv at ${RL_VENV} ..." >&2
  python3 -m venv "${RL_VENV}" --without-pip
  curl -sS https://bootstrap.pypa.io/get-pip.py | "${RL_VENV}/bin/python3" >/dev/null 2>&1 || \
    python3 -m ensurepip --root "${RL_VENV}" 2>/dev/null || true
fi

# Ensure required packages are in the venv.
# `kubernetes` is required for the persistent CustomObjectsApi client.
if ! "${RL_VENV}/bin/python3" -c "import stable_baselines3, kubernetes" >/dev/null 2>&1; then
  echo "[rl-v1] installing SB3 + torch + kubernetes into venv (this may take a minute)..." >&2
  "${RL_VENV}/bin/pip" install --quiet \
    "stable-baselines3>=2.0" \
    "torch" \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "numpy>=2.0" matplotlib pandas gymnasium pyyaml psutil \
    "kubernetes>=29" requests
fi

# Prepend venv to PATH so every python3 call in subprocesses uses the venv.
export PATH="${RL_VENV}/bin:${PATH}"
echo "[rl-v1] python: $(which python3)  numpy: $(python3 -c 'import numpy; print(numpy.__version__)')" >&2

python3 -c "import yaml" >/dev/null 2>&1 || {
  echo "pyyaml is required: python3 -m pip install --user pyyaml" >&2
  exit 1
}

RUN_TS="$(date +%Y%m%d_%H%M%S)"
PLAN_FILE="$(mktemp)"
ENV_FILE="$(mktemp)"
EXTRA_BASELINES_FILE="$(mktemp)"
trap 'rm -f "${PLAN_FILE}" "${ENV_FILE}" "${EXTRA_BASELINES_FILE}"' EXIT

python3 - "${CONFIG_PATH}" "${SCENARIO_SET}" "${SCENARIO_IDS}" "${OUTPUT_ROOT}" "${RUN_TS}" "${EXTRA_BASELINES_FILE}" >"${PLAN_FILE}" 3>"${ENV_FILE}" <<'PY'
import json
import os
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1]).resolve()
scenario_set = sys.argv[2]
ids_arg = sys.argv[3]
output_root_arg = sys.argv[4]
run_ts = sys.argv[5]
extra_baselines_path = Path(sys.argv[6])

cfg = yaml.safe_load(config_path.read_text())
exp = cfg["experiment"]
controller = cfg["controller"]
resources = cfg.get("resource_sampling", {})

scenarios = cfg["scenarios"]["core"]
by_id = {str(s["id"]): s for s in scenarios}
if ids_arg:
    selected = [by_id[i] for i in ids_arg.split(",") if i in by_id]
    missing = [i for i in ids_arg.split(",") if i not in by_id]
    if missing:
        raise SystemExit(f"unknown scenario id(s): {','.join(missing)}")
else:
    wanted = cfg["scenarios"].get(scenario_set)
    if wanted is None:
        raise SystemExit(f"unknown scenario set: {scenario_set}")
    selected = [by_id[i] for i in wanted] if all(isinstance(x, str) for x in wanted) else wanted

out_root = output_root_arg or cfg.get("output_root", "outputs/prototype/rb-rl-v1")
out_root_path = Path(out_root)
if not out_root_path.is_absolute():
    out_root_path = Path.cwd() / out_root_path
out_root_path = out_root_path / run_ts

def _abs(p: str) -> str:
    path = Path(p)
    return str(path if path.is_absolute() else Path.cwd() / path)


# extra_baselines: list of {root, label} layered into the plot command
# after the primary baseline. Written to a sidecar file (one root\tlabel
# per line) instead of an env var so we don't have to escape tabs / spaces
# / non-ASCII through json+bash quoting.
extra = cfg.get("extra_baselines") or []
with extra_baselines_path.open("w") as f:
    for item in extra:
        if not isinstance(item, dict) or "root" not in item:
            raise SystemExit(
                f"extra_baselines entry must be a dict with 'root': {item!r}"
            )
        root = _abs(str(item["root"]))
        label = str(item.get("label") or Path(root).name)
        if "\t" in root or "\t" in label or "\n" in root or "\n" in label:
            raise SystemExit(
                f"extra_baselines root/label cannot contain TAB or newline: {item!r}"
            )
        f.write(f"{root}\t{label}\n")

env = {
    "RUN_ROOT": str(out_root_path),
    "BASELINE_ROOT": _abs(cfg["baseline_root"]),
    "BASELINE_LABEL": str(cfg.get("baseline_label") or "Baseline"),
    "RUN_LABEL": str(cfg.get("label") or cfg.get("name") or "Current"),
    "SCENARIO_NAME": exp["scenario"],
    "POLICY": exp["policy"],
    "CLIENT_PROFILE": exp["client_profile"],
    "WARMUP": str(exp["warmup"]),
    "PREFAULT": str(exp["prefault"]),
    "RECOVERY": str(exp["recovery"]),
    "COOLDOWN": str(exp["cooldown"]),
    "SETTLE": str(exp.get("settle", 30)),
    "NUM_LOADERS": str(exp.get("num_loaders", 1)),
    "RESOURCE_INTERVAL": str(resources.get("interval_sec", 2)),
    "RESOURCE_ENABLED": "true" if resources.get("enabled", True) else "false",
    "CONFIG_ABS": str(config_path),
}
# fd 3 is the env file; stdout is the scenario TSV.
with os.fdopen(3, "w") as f:
    for key, value in env.items():
        f.write(f"{key}={json.dumps(value)}\n")

for s in selected:
    # Scenario-cell label. Same shape as rb-mega-fault-sweep cells so
    # plot_rl_comparison.py can match RL cells to baseline cells directly.
    label = (
        f"rate_rps={s['rate_rps']}__"
        f"fault_duration={s['fault_duration']}__"
        f"fault_rate=cartservice-{s['fault_rate']}pct"
    )
    print("\t".join([
        str(s["id"]),
        str(s["rate_rps"]),
        str(s["fault_duration"]),
        str(s["fault_rate"]),
        str(s.get("default_label", "")),
        label,
    ]))
PY

# shellcheck source=/dev/null
source "${ENV_FILE}"
[[ -n "${NUM_LOADERS_OVERRIDE}" ]] && NUM_LOADERS="${NUM_LOADERS_OVERRIDE}"

PROFILE_PATH="${PROTO_DIR}/clients/online-boutique/profiles/${CLIENT_PROFILE}.json"
[[ -f "${PROFILE_PATH}" ]] || { echo "client profile not found: ${PROFILE_PATH}" >&2; exit 1; }

PROFILE_BACKUP="$(mktemp)"
cp "${PROFILE_PATH}" "${PROFILE_BACKUP}"
restore_profile() {
  cp "${PROFILE_BACKUP}" "${PROFILE_PATH}" 2>/dev/null || true
  rm -f "${PROFILE_BACKUP}"
}
trap 'restore_profile; rm -f "${PLAN_FILE}" "${ENV_FILE}"' EXIT

mkdir -p "${RUN_ROOT}"
cp "${CONFIG_ABS}" "${RUN_ROOT}/$(basename "${CONFIG_ABS}")"

echo "[rl-v1] output root: ${RUN_ROOT}"
echo "[rl-v1] baseline root: ${BASELINE_ROOT}"
echo "[rl-v1] scenario set: ${SCENARIO_SET}${SCENARIO_IDS:+ ids=${SCENARIO_IDS}}"

while IFS=$'\t' read -r scenario_id rate_rps fault_duration fault_rate default_label label; do
  [[ -n "${scenario_id}" ]] || continue
  echo
  echo "[rl-v1] ${scenario_id}: ${label} default=${default_label}"

  python3 - "${PROFILE_PATH}" "${rate_rps}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rate = int(sys.argv[2])
doc = json.loads(path.read_text())
doc["rate_rps"] = rate
path.write_text(json.dumps(doc, indent=2) + "\n")
PY

  out_dir="${RUN_ROOT}/${label}"
  fault_manifest="cartservice-${fault_rate}pct"
  cmd=(
    "${SCRIPT_DIR}/run-experiment.sh"
    --scenario "${SCENARIO_NAME}"
    --policies "${POLICY}"
    --client-profiles "${CLIENT_PROFILE}"
    --fault-manifest "${fault_manifest}"
    --warmup "${WARMUP}"
    --prefault "${PREFAULT}"
    --fault "${fault_duration}"
    --recovery "${RECOVERY}"
    --cooldown "${COOLDOWN}"
    --settle "${SETTLE}"
    --output "${out_dir}"
    --rl-controller-config "${CONFIG_ABS}"
    --resource-sample-interval "${RESOURCE_INTERVAL}"
  )
  [[ "${RESOURCE_ENABLED}" == "true" ]] && cmd+=(--resource-sampling)
  [[ "${SHADOW_MODE}" == "true" ]] && cmd+=(--rl-shadow)
  [[ -n "${RL_IMAGE_TAG}" ]] && cmd+=(--rl-image-tag "${RL_IMAGE_TAG}")
  [[ -n "${RL_CONFIGMAP}" ]] && cmd+=(--rl-configmap "${RL_CONFIGMAP}")
  [[ -n "${RL_LOADER_PORT_BASE}" ]] && cmd+=(--rl-loader-port-base "${RL_LOADER_PORT_BASE}")
  [[ -n "${RL_LOADER_HOST}" ]] && cmd+=(--rl-loader-host "${RL_LOADER_HOST}")
  [[ -n "${RL_OBS_MODE}" ]] && cmd+=(--rl-obs-mode "${RL_OBS_MODE}")
  [[ "${DRY_RUN}" == "true" ]] && cmd+=(--dry-run)

  echo "[rl-v1] command: NUM_LOADERS=${NUM_LOADERS} ${cmd[*]}"
  NUM_LOADERS="${NUM_LOADERS}" "${cmd[@]}" </dev/null
done < "${PLAN_FILE}"

restore_profile

if [[ "${SKIP_PLOTS}" == "false" && "${DRY_RUN}" == "false" ]]; then
  # The first --policy is the "current" run; comparison plots are written
  # back into its per-scenario plots/ folders. Add the baseline second.
  plot_cmd=(
    python3 "${SCRIPT_DIR}/plot_rl_comparison.py"
    --policy "${RUN_ROOT}" "${RUN_LABEL}"
  )
  [[ -n "${BASELINE_ROOT}" ]] && plot_cmd+=(--policy "${BASELINE_ROOT}" "${BASELINE_LABEL}")
  # Append extra baselines from the sidecar file (one root\tlabel per line).
  if [[ -s "${EXTRA_BASELINES_FILE}" ]]; then
    while IFS=$'\t' read -r _root _label; do
      [[ -n "${_root}" ]] || continue
      plot_cmd+=(--policy "${_root}" "${_label}")
    done < "${EXTRA_BASELINES_FILE}"
  fi
  echo "[rl-v1] plot command: ${plot_cmd[*]}"
  "${plot_cmd[@]}" || echo "[rl-v1] comparison plotting failed" >&2

fi

echo "[rl-v1] done: ${RUN_ROOT}"
