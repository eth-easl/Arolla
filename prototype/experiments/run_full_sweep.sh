#!/usr/bin/env bash
# Run the 25-scenario benchmark grid for all 8 control policies on the
# new (Phase 4) infrastructure. Output root is outputs/prototype-new/
# unless overridden.
#
# Per scenario, this script:
#   1. Calls run-experiment.sh once with the 4 non-RL baselines.
#   2. Calls run-experiment.sh once per RL variant (4 calls), then renames
#      each variant's envoy-retry-budget/ dir into rb-rl-vX/.
#   3. Rewrites summary.csv and experiment.json to include the RL rows.
#   4. Calls aggregate_for_viewer.py on the scenario dir.
#
# Resumption: skips any scenario where every required policy dir already
# exists. Re-running after a partial failure is safe and idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

OUTPUT_ROOT="outputs/prototype-new/full-sweep"
SWEEP_TS=""
SCENARIO_IDS=""              # optional comma-separated S01,S02,... to subset
POLICY_FILTER=""             # optional comma-separated allowlist
DRY_RUN=false
SKIP_AGGREGATE=false
RL_IMAGE_TAG="v3.3"
RL_LOADER_PORT_BASE="8765"

NON_RL_POLICIES="no-control,circuit-breaker,envoy-retry-budget,arolla"
RL_VARIANTS=(
  "rb-rl-v1-a:rl_configs/v1/rb-rl-v1-a.yaml:rl-controller-v1-a"
  "rb-rl-v1-b:rl_configs/v1/rb-rl-v1-b.yaml:rl-controller-v1-b"
  "rb-rl-v2:rl_configs/v2/rb-rl-v2.yaml:rl-controller-v2"
  "rb-rl-v3:rl_configs/v3/rb-rl-v3.yaml:rl-controller-v3"
  "rb-rl-v4:rl_configs/v4/rb-rl-v4.yaml:rl-controller-v4"
  "rb-rl-v5:rl_configs/v5/rb-rl-v5.yaml:rl-controller-v5"
)

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --output-root <dir>     Output root (default: ${OUTPUT_ROOT})
  --sweep-ts <ts>         Sweep timestamp dir (default: $(date +%Y%m%d_%H%M%S))
  --ids <S01,S02,...>     Subset of scenarios from sweep_scenarios.csv
  --policies <a,b,...>    Subset of {${NON_RL_POLICIES},rb-rl-v1-a,rb-rl-v1-b,rb-rl-v2,rb-rl-v3,rb-rl-v4,rb-rl-v5}
  --rl-image-tag <tag>    Image tag for in-cluster RL controllers (default: ${RL_IMAGE_TAG})
  --rl-port-base <n>      Loader /window port base (default: ${RL_LOADER_PORT_BASE})
  --skip-aggregate        Skip the aggregate_for_viewer.py step
  -n, --dry-run           Print planned invocations without running
  -h, --help              Show this message
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
    --sweep-ts)    SWEEP_TS="$2"; shift 2 ;;
    --ids)         SCENARIO_IDS="$2"; shift 2 ;;
    --policies)    POLICY_FILTER="$2"; shift 2 ;;
    --rl-image-tag) RL_IMAGE_TAG="$2"; shift 2 ;;
    --rl-port-base) RL_LOADER_PORT_BASE="$2"; shift 2 ;;
    --skip-aggregate) SKIP_AGGREGATE=true; shift ;;
    -n|--dry-run)  DRY_RUN=true; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "${SWEEP_TS}" ]] || SWEEP_TS="$(date +%Y%m%d_%H%M%S)"
SWEEP_DIR="${OUTPUT_ROOT}/${SWEEP_TS}"
mkdir -p "${SWEEP_DIR}"

# In-cluster RL Jobs mount ConfigMaps (e.g. rl-controller-v1-a). One-shot
# cluster setup often only created rl-controller-v3 — apply all variants
# from rl_configs/ so FailedMount does not surface mid-sweep.
if ! "${DRY_RUN}"; then
  "${SCRIPT_DIR}/ensure_rl_configmaps.sh"
fi

SCENARIOS_CSV="${SCRIPT_DIR}/sweep_scenarios.csv"
[[ -f "${SCENARIOS_CSV}" ]] || { echo "missing ${SCENARIOS_CSV}" >&2; exit 1; }

# Wanted-IDs set as a space-padded string for bash 3.2 compatibility
# (no associative arrays). Empty when --ids was not passed.
WANTED_IDS_SET=""
if [[ -n "${SCENARIO_IDS}" ]]; then
  WANTED_IDS_SET=" ${SCENARIO_IDS//,/ } "
fi

scenario_label() {  # $1=rps $2=fd $3=fr
  printf "rate_rps=%s__fault_duration=%s__fault_rate=cartservice-%spct" "$1" "$2" "$3"
}

profile_for_rps() {                # $1=rps; updates the client profile in-place
  local rps="$1"
  local profile="${PROTO_DIR}/clients/online-boutique/profiles/post-cart-stress-open.json"
  if "${DRY_RUN}"; then
    echo "  [profile] would set rate_rps=${rps} in ${profile##*/}"
    return 0
  fi
  python3 - "${profile}" "${rps}" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
doc = json.loads(p.read_text())
doc["rate_rps"] = int(sys.argv[2])
p.write_text(json.dumps(doc, indent=2) + "\n")
PY
}

run_baselines() {                  # $1=scenario_label $2=fault_duration $3=fault_rate $4=out_dir
  local label="$1" fd="$2" fr="$3" out="$4"
  # Skip if every non-RL policy dir already exists (resumable).
  local skip=true
  IFS=',' read -r -a _pols <<< "${NON_RL_POLICIES}"
  for p in "${_pols[@]}"; do
    [[ -d "${out}/${p}" ]] || skip=false
  done
  if "${skip}"; then
    echo "  [skip] baselines already present in ${out}"
    return 0
  fi

  local cmd=(
    "${SCRIPT_DIR}/run-experiment.sh"
    --scenario sustained-failure
    --policies "${NON_RL_POLICIES}"
    --client-profiles post-cart-stress-open
    --fault-manifest "cartservice-${fr}pct"
    --fault "${fd}"
    --warmup 30 --prefault 60 --recovery 180 --cooldown 10
    --settle 30
    --output "${out}"
    --resource-sampling --resource-sample-interval 2
  )
  echo "  [baselines] ${cmd[*]}"
  "${DRY_RUN}" || NUM_LOADERS=4 "${cmd[@]}"
}

run_rl_variant() {                 # $1=variant_name $2=config_relpath $3=configmap $4=out_dir
  local variant="$1" cfg_rel="$2" cm="$3" scen_dir="$4"
  if [[ -d "${scen_dir}/${variant}" ]]; then
    echo "  [skip] ${variant} already present in ${scen_dir}"
    return 0
  fi
  local cfg_path="${SCRIPT_DIR}/${cfg_rel}"
  [[ -f "${cfg_path}" ]] || { echo "  [error] missing RL config: ${cfg_path}" >&2; return 1; }

  local fd fr
  fd="$(basename "${scen_dir}" | sed -n 's/.*fault_duration=\([0-9]*\).*/\1/p')"
  fr="$(basename "${scen_dir}" | sed -n 's/.*fault_rate=cartservice-\([0-9]*\)pct.*/\1/p')"

  local tmp_out
  tmp_out="$(mktemp -d)"
  local cmd=(
    "${SCRIPT_DIR}/run-experiment.sh"
    --scenario sustained-failure
    --policies envoy-retry-budget
    --client-profiles post-cart-stress-open
    --fault-manifest "cartservice-${fr}pct"
    --fault "${fd}"
    --warmup 30 --prefault 60 --recovery 180 --cooldown 10
    --settle 30
    --output "${tmp_out}"
    --rl-controller-config "${cfg_path}"
    --rl-in-cluster
    --rl-image-tag "${RL_IMAGE_TAG}"
    --rl-configmap "${cm}"
    --rl-loader-port-base "${RL_LOADER_PORT_BASE}"
    --rl-obs-mode auto
    --resource-sampling --resource-sample-interval 2
  )
  echo "  [${variant}] ${cmd[*]}"
  if "${DRY_RUN}"; then
    rm -rf "${tmp_out}"; return 0
  fi
  NUM_LOADERS=4 "${cmd[@]}"
  # The RL run produces ${tmp_out}/envoy-retry-budget/<files…>. Move it into
  # the canonical scenario dir under the RL variant name.
  mv "${tmp_out}/envoy-retry-budget" "${scen_dir}/${variant}"
  merge_rl_into_scenario "${variant}" "${tmp_out}" "${scen_dir}"
  rm -rf "${tmp_out}"
}

merge_rl_into_scenario() {         # $1=variant_name $2=tmp_run_dir $3=scen_dir
  local variant="$1" tmp_out="$2" scen_dir="$3"
  python3 - "${variant}" "${tmp_out}" "${scen_dir}" <<'PY'
import json, sys, csv
from pathlib import Path

variant, tmp_out, scen_dir = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
src_summary = tmp_out / "summary.csv"
dst_summary = scen_dir / "summary.csv"
if src_summary.exists() and dst_summary.exists():
    with src_summary.open() as f:
        src_rows = list(csv.DictReader(f))
    with dst_summary.open() as f:
        reader = csv.DictReader(f)
        dst_rows = list(reader)
        header = list(reader.fieldnames or [])
    # Replace the RL row's policy slug with the variant name
    for r in src_rows:
        r["policy"] = variant
    # Drop any prior row with this variant name (idempotent reruns)
    dst_rows = [r for r in dst_rows if r.get("policy") != variant]
    dst_rows.extend(src_rows)
    fields = list({k for r in dst_rows for k in r.keys()})
    # Preserve dst header order, append new fields
    seen = set(header); out_fields = list(header) + [f for f in fields if f not in seen]
    with dst_summary.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        for r in dst_rows:
            w.writerow({k: r.get(k, "") for k in out_fields})

src_exp = tmp_out / "experiment.json"
dst_exp = scen_dir / "experiment.json"
if src_exp.exists() and dst_exp.exists():
    src = json.loads(src_exp.read_text())
    dst = json.loads(dst_exp.read_text())
    dst.setdefault("policies", [])
    if variant not in dst["policies"]:
        dst["policies"].append(variant)
    dst.setdefault("policies_spec", [])
    src_spec = next((s for s in src.get("policies_spec", []) if s.get("name") == "envoy-retry-budget"), None)
    if src_spec is not None:
        renamed = dict(src_spec); renamed["name"] = variant
        dst["policies_spec"] = [s for s in dst["policies_spec"] if s.get("name") != variant]
        dst["policies_spec"].append(renamed)
    dst_exp.write_text(json.dumps(dst, indent=2))
PY
}

echo "[full-sweep] output dir: ${SWEEP_DIR}"
echo "[full-sweep] non-RL policies: ${NON_RL_POLICIES}"
echo "[full-sweep] RL variants: ${RL_VARIANTS[*]%%:*}"

# Scenario loop body lands in the next task.
# Use fd 3 for the CSV so that stdin (fd 0) is never consumed by ssh or other
# commands that inherit stdin inside the loop body.
while IFS=, read -r id rps fd fr default_label <&3; do
  [[ "${id}" == "id" ]] && continue
  [[ -z "${id}" ]] && continue
  if [[ -n "${WANTED_IDS_SET}" && "${WANTED_IDS_SET}" != *" ${id} "* ]]; then continue; fi
  label="$(scenario_label "${rps}" "${fd}" "${fr}")"
  echo "[full-sweep] ${id}: ${label} (default=${default_label})"
  scen_dir="${SWEEP_DIR}/${label}"
  mkdir -p "${scen_dir}"
  if [[ -z "${POLICY_FILTER}" ]] || [[ ",${POLICY_FILTER}," == *",no-control,"* ]] \
     || [[ ",${POLICY_FILTER}," == *",circuit-breaker,"* ]] \
     || [[ ",${POLICY_FILTER}," == *",envoy-retry-budget,"* ]] \
     || [[ ",${POLICY_FILTER}," == *",arolla,"* ]]; then
    profile_for_rps "${rps}"
    run_baselines "${label}" "${fd}" "${fr}" "${scen_dir}"
  fi
  for spec in "${RL_VARIANTS[@]}"; do
    variant="${spec%%:*}"
    rest="${spec#*:}"
    cfg_rel="${rest%%:*}"
    cm="${rest#*:}"
    if [[ -n "${POLICY_FILTER}" && ",${POLICY_FILTER}," != *",${variant},"* ]]; then continue; fi
    profile_for_rps "${rps}"
    run_rl_variant "${variant}" "${cfg_rel}" "${cm}" "${scen_dir}"
  done
  if ! "${SKIP_AGGREGATE}" && ! "${DRY_RUN}"; then
    echo "  [aggregate] python3 ${SCRIPT_DIR}/aggregate_for_viewer.py ${scen_dir}"
    python3 "${SCRIPT_DIR}/aggregate_for_viewer.py" "${scen_dir}" \
      || echo "  [warn] aggregation failed for ${label}"
  fi
done 3< "${SCENARIOS_CSV}"
