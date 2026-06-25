#!/usr/bin/env bash
# Run the 30-scenario benchmark grid for the 4 Phase-B control policies
# (no-control, envoy-retry-budget, arolla, rb-rl-v5), with N repeats for
# noise-averaging. Output root is outputs/proto-report/ unless overridden.
#
# Scenarios come from TWO files:
#   * sweep_scenarios.csv      — the canonical 25 (cartservice, single spike,
#                                post-cart-stress-open client).
#   * sweep_scenarios_ext.csv  — the 5 new ones (per-row client profile, fault
#                                manifest, num_spikes, inter_spike_gap, and the
#                                RL callee/caller-labels for alt-service runs).
#
# Per (repeat, scenario) this script:
#   1. Calls run-experiment.sh once with the 3 non-RL baselines.
#   2. Calls run-experiment.sh once for rb-rl-v5, then renames the variant's
#      envoy-retry-budget/ dir into rb-rl-v5/ and merges its rows.
#   3. Calls aggregate_for_viewer.py on the scenario dir (best-effort).
#
# Output layout: <output_root>/<sweep_ts>/run{1..N}/<scenario_label>/<policy>/…
#
# Resumption: skips any (repeat, scenario, policy) whose timeline.json already
# exists; stale/empty policy dirs from interrupted runs are cleared and re-run.
# Re-running after a partial failure is safe and idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

OUTPUT_ROOT="outputs/proto-report"
SWEEP_TS=""
SCENARIO_IDS=""              # optional comma-separated S01,MS1,... to subset
POLICY_FILTER=""             # optional comma-separated allowlist
REPEATS=1
DRY_RUN=false
SKIP_AGGREGATE=false
RL_IMAGE_TAG="v5"
RL_LOADER_PORT_BASE="8765"

NON_RL_POLICIES="no-control,envoy-retry-budget,arolla"
RL_VARIANTS=(
  "rb-rl-v5:rl_configs/v5/rb-rl-v5.yaml:rl-controller-v5"
)

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --output-root <dir>     Output root (default: ${OUTPUT_ROOT})
  --sweep-ts <ts>         Sweep timestamp dir (default: $(date +%Y%m%d_%H%M%S))
  --repeats <n>           Number of repeat runs to average over (default: ${REPEATS}).
                          Output goes to <sweep_ts>/run1, run2, ...
  --ids <S01,MS1,...>     Subset of scenarios (ids from either CSV)
  --policies <a,b,...>    Subset of {${NON_RL_POLICIES},rb-rl-v5}
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
    --repeats)     REPEATS="$2"; shift 2 ;;
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

# In-cluster RL Jobs mount ConfigMaps (e.g. rl-controller-v5). One-shot cluster
# setup may only have created a subset — apply all variants from rl_configs/ so
# FailedMount does not surface mid-sweep.
if ! "${DRY_RUN}"; then
  "${SCRIPT_DIR}/ensure_rl_configmaps.sh"
fi

SCENARIOS_CSV="${SCRIPT_DIR}/sweep_scenarios.csv"
SCENARIOS_EXT_CSV="${SCRIPT_DIR}/sweep_scenarios_ext.csv"
[[ -f "${SCENARIOS_CSV}" ]] || { echo "missing ${SCENARIOS_CSV}" >&2; exit 1; }

# Wanted-IDs set as a space-padded string for bash 3.2 compatibility
# (no associative arrays). Empty when --ids was not passed.
WANTED_IDS_SET=""
if [[ -n "${SCENARIO_IDS}" ]]; then
  WANTED_IDS_SET=" ${SCENARIO_IDS//,/ } "
fi

# Label encodes target service + fault severity (via the manifest token) and,
# for multi-spike runs, the spike count and inter-spike gap. The report builder
# and this driver parse it; the plot-viewer does not.
scenario_label() {  # $1=rps $2=fd $3=fault_token $4=num_spikes $5=gap
  local base
  base="rate_rps=$1__fault_duration=$2__fault_rate=$3"
  if (( ${4:-1} > 1 )); then
    base="${base}__spikes=$4__gap=$5"
  fi
  printf "%s" "${base}"
}

profile_for_rps() {                # $1=profile_name $2=rps; edits the profile in place
  local profile_name="$1" rps="$2"
  local profile="${PROTO_DIR}/clients/online-boutique/profiles/${profile_name}.json"
  if "${DRY_RUN}"; then
    echo "  [profile] would set rate_rps=${rps} in ${profile_name}.json"
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

run_baselines() {  # $1=out_dir $2=profile $3=manifest $4=fd $5=num_spikes $6=gap
  local out="$1" profile="$2" manifest="$3" fd="$4" nsp="$5" gap="$6"
  # Resume granularity is per-policy: a policy is "done" only if its
  # timeline.json exists. An interrupted run can leave an empty/partial policy
  # dir behind, so checking dir existence alone (the old behaviour) wrongly
  # marked those as complete and skipped them forever. Re-run only the policies
  # whose timeline.json is missing, clearing any stale partial dir first so
  # run-experiment.sh writes clean. Sibling (already-complete) policy dirs are
  # left untouched — run-experiment.sh writes each policy to its own subdir.
  IFS=',' read -r -a _pols <<< "${NON_RL_POLICIES}"
  local missing=()
  for p in "${_pols[@]}"; do
    if [[ -f "${out}/${p}/timeline.json" ]]; then
      continue
    fi
    if ! "${DRY_RUN}" && [[ -d "${out}/${p}" ]]; then
      rm -rf "${out}/${p}"
    fi
    missing+=("${p}")
  done
  if (( ${#missing[@]} == 0 )); then
    echo "  [skip] baselines already present in ${out}"
    return 0
  fi
  local policies_csv
  policies_csv="$(IFS=,; printf '%s' "${missing[*]}")"

  local cmd=(
    "${SCRIPT_DIR}/run-experiment.sh"
    --scenario sustained-failure
    --policies "${policies_csv}"
    --client-profiles "${profile}"
    --fault-manifest "${manifest}"
    --fault "${fd}"
    --num-spikes "${nsp}" --inter-spike-gap "${gap}"
    --warmup 30 --prefault 60 --recovery 180 --cooldown 10
    --settle 30
    --output "${out}"
    --resource-sampling --resource-sample-interval 2
  )
  echo "  [baselines] (missing: ${policies_csv}) ${cmd[*]}"
  "${DRY_RUN}" || NUM_LOADERS=4 "${cmd[@]}"
}

run_rl_variant() {  # $1=variant $2=cfg_rel $3=cm $4=scen_dir $5=profile $6=manifest
                    # $7=fd $8=num_spikes $9=gap $10=callee $11=caller_labels
  local variant="$1" cfg_rel="$2" cm="$3" scen_dir="$4" profile="$5" manifest="$6"
  local fd="$7" nsp="$8" gap="$9" callee="${10}" caller_labels="${11}"
  # Per-policy resume: done only if timeline.json exists. A stale/empty variant
  # dir (left by an interrupted run) would otherwise be skipped forever AND make
  # the mv below nest the new run inside it, so clear it before re-running.
  if [[ -f "${scen_dir}/${variant}/timeline.json" ]]; then
    echo "  [skip] ${variant} already present in ${scen_dir}"
    return 0
  fi
  if ! "${DRY_RUN}" && [[ -d "${scen_dir}/${variant}" ]]; then
    rm -rf "${scen_dir}/${variant}"
  fi
  local cfg_path="${SCRIPT_DIR}/${cfg_rel}"
  [[ -f "${cfg_path}" ]] || { echo "  [error] missing RL config: ${cfg_path}" >&2; return 1; }

  local tmp_out
  tmp_out="$(mktemp -d)"
  local cmd=(
    "${SCRIPT_DIR}/run-experiment.sh"
    --scenario sustained-failure
    --policies envoy-retry-budget
    --client-profiles "${profile}"
    --fault-manifest "${manifest}"
    --fault "${fd}"
    --num-spikes "${nsp}" --inter-spike-gap "${gap}"
    --warmup 30 --prefault 60 --recovery 180 --cooldown 10
    --settle 30
    --output "${tmp_out}"
    --rl-controller-config "${cfg_path}"
    --rl-in-cluster
    --rl-image-tag "${RL_IMAGE_TAG}"
    --rl-configmap "${cm}"
    --rl-loader-port-base "${RL_LOADER_PORT_BASE}"
    --rl-obs-mode auto
    --rl-callee "${callee}"
    --rl-caller-labels "${caller_labels}"
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

baselines_wanted() {  # returns 0 if any baseline policy is in the filter
  [[ -z "${POLICY_FILTER}" ]] && return 0
  IFS=',' read -r -a _pols <<< "${NON_RL_POLICIES}"
  for p in "${_pols[@]}"; do
    [[ ",${POLICY_FILTER}," == *",${p},"* ]] && return 0
  done
  return 1
}

# Run one scenario cell within a given repeat dir.
run_scenario() {  # $1=run_dir $2=id $3=profile $4=manifest $5=rps $6=fd
                  # $7=num_spikes $8=gap $9=callee $10=caller_labels $11=default_label
  local run_dir="$1" id="$2" profile="$3" manifest="$4" rps="$5" fd="$6"
  local nsp="$7" gap="$8" callee="$9" caller_labels="${10}" default_label="${11}"

  if [[ -n "${WANTED_IDS_SET}" && "${WANTED_IDS_SET}" != *" ${id} "* ]]; then
    return 0
  fi

  local label
  label="$(scenario_label "${rps}" "${fd}" "${manifest}" "${nsp}" "${gap}")"
  echo "[full-sweep] ${id}: ${label} (default=${default_label})"
  local scen_dir="${run_dir}/${label}"
  mkdir -p "${scen_dir}"

  if baselines_wanted; then
    profile_for_rps "${profile}" "${rps}"
    run_baselines "${scen_dir}" "${profile}" "${manifest}" "${fd}" "${nsp}" "${gap}"
  fi

  for spec in "${RL_VARIANTS[@]}"; do
    local variant="${spec%%:*}" rest="${spec#*:}"
    local cfg_rel="${rest%%:*}" cm="${rest#*:}"
    if [[ -n "${POLICY_FILTER}" && ",${POLICY_FILTER}," != *",${variant},"* ]]; then continue; fi
    profile_for_rps "${profile}" "${rps}"
    run_rl_variant "${variant}" "${cfg_rel}" "${cm}" "${scen_dir}" \
      "${profile}" "${manifest}" "${fd}" "${nsp}" "${gap}" "${callee}" "${caller_labels}"
  done

  if ! "${SKIP_AGGREGATE}" && ! "${DRY_RUN}"; then
    echo "  [aggregate] python3 ${SCRIPT_DIR}/aggregate_for_viewer.py ${scen_dir}"
    python3 "${SCRIPT_DIR}/aggregate_for_viewer.py" "${scen_dir}" \
      || echo "  [warn] aggregation failed for ${label}"
  fi
}

echo "[full-sweep] output dir: ${SWEEP_DIR}"
echo "[full-sweep] repeats: ${REPEATS}"
echo "[full-sweep] non-RL policies: ${NON_RL_POLICIES}"
echo "[full-sweep] RL variants: ${RL_VARIANTS[*]%%:*}"

# Pre-flatten the extended CSV to TSV so the quoted caller-labels field (which
# contains commas) survives shell field-splitting. Empty if the file is absent.
EXT_TSV=""
if [[ -f "${SCENARIOS_EXT_CSV}" ]]; then
  EXT_TSV="$(python3 - "${SCENARIOS_EXT_CSV}" <<'PY'
import csv, sys
cols = ["id","client_profile","fault_manifest","rate_rps","fault_duration",
        "fault_rate","num_spikes","inter_spike_gap","rl_callee",
        "rl_caller_labels","default_label"]
with open(sys.argv[1], newline="") as f:
    for r in csv.DictReader(f):
        print("\t".join(r[c] for c in cols))
PY
)"
fi

for (( run_idx=1; run_idx<=REPEATS; run_idx++ )); do
  RUN_DIR="${SWEEP_DIR}/run${run_idx}"
  mkdir -p "${RUN_DIR}"
  echo "[full-sweep] ===== repeat ${run_idx}/${REPEATS} → ${RUN_DIR} ====="

  # --- canonical 25 (cartservice, single spike, post-cart-stress-open) ---
  # fd 3 keeps stdin (fd 0) free for ssh and friends inside the loop body.
  while IFS=, read -r id rps fd fr default_label <&3; do
    [[ "${id}" == "id" ]] && continue
    [[ -z "${id}" ]] && continue
    run_scenario "${RUN_DIR}" "${id}" "post-cart-stress-open" \
      "cartservice-${fr}pct" "${rps}" "${fd}" 1 0 \
      "cartservice" "frontend,checkoutservice" "${default_label}" \
      || echo "[full-sweep][warn] scenario ${id} failed in repeat ${run_idx}; continuing (resumable)"
  done 3< "${SCENARIOS_CSV}"

  # --- the 5 new scenarios (per-row profile / manifest / spikes / RL target) ---
  # Read via fd 3 (like the canonical loop) so ssh inside run-experiment.sh
  # cannot swallow the remaining rows from stdin and end the loop early.
  if [[ -n "${EXT_TSV}" ]]; then
    while IFS=$'\t' read -r id profile manifest rps fd fr nsp gap callee labels default_label <&3; do
      [[ -z "${id}" ]] && continue
      run_scenario "${RUN_DIR}" "${id}" "${profile}" "${manifest}" "${rps}" "${fd}" \
        "${nsp}" "${gap}" "${callee}" "${labels}" "${default_label}" \
        || echo "[full-sweep][warn] scenario ${id} failed in repeat ${run_idx}; continuing (resumable)"
    done 3<<< "${EXT_TSV}"
  fi
done
