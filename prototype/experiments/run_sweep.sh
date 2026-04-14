#!/usr/bin/env bash
# ==========================================================================
# run_sweep.sh — run parameter sweeps defined in a YAML config
# ==========================================================================
#
# Reads a sweep config (e.g. sweeps/sensitivity.yaml) and for each sweep
# listed, iterates over the parameter values, edits the target file in
# place, runs run-experiment.sh, and restores the original file.
#
# Usage:
#   ./run_sweep.sh sweeps/sensitivity.yaml                # all sweeps
#   ./run_sweep.sh sweeps/sensitivity.yaml fault-duration  # one sweep
#   ./run_sweep.sh sweeps/sensitivity.yaml arolla-r arolla-c
#
# The second+ arguments are sweep names to run. If omitted, ALL sweeps
# in the config are run sequentially.
#
# Output directory is derived from the sweep config filename:
#   sweeps/sensitivity.yaml → outputs/prototype/sensitivity/<timestamp>/<profile>/<value>/
#   sweeps/rps_sweep.yaml   → outputs/prototype/rps_sweep/<timestamp>/<profile>/<value>/
#
# Requirements:
#   - Python 3 with pyyaml (`pip install pyyaml`)
#   - run-experiment.sh on PATH (this script invokes it by relative path)
#   - kubectl configured for the target cluster
#
# ==========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"

CONFIG="${1:?usage: $0 <sweep-config.yaml> [sweep-name ...]}"
shift
SELECTED_SWEEPS=("$@")  # empty = run all

# Derive the output directory name from the sweep config filename.
# sweeps/sensitivity.yaml → "sensitivity"
# sweeps/rps_sweep.yaml   → "rps_sweep"
SWEEP_STEM="$(basename "${CONFIG}" .yaml)"
BATCH_TS="$(date +%Y%m%d_%H%M%S)"
OUTPUT_ROOT="${REPO_ROOT}/outputs/prototype/${SWEEP_STEM}/${BATCH_TS}"

log()  { printf '\033[1;34m[sweep]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[sweep]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[sweep]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Parse the YAML config into a set of shell-friendly variables via Python.
# For each sweep we extract: name, policies, parameter details, base config.
# --------------------------------------------------------------------------

SWEEP_JSON=$(python3 - "$CONFIG" <<'PYEOF'
import yaml, json, sys
config = yaml.safe_load(open(sys.argv[1]))
# Flatten defaults into each sweep's base (the YAML anchor does this
# already at parse time, but be safe).
defaults = config.get("defaults", {})
sweeps = []
for s in config["sweeps"]:
    base = dict(defaults)
    base.update(s.get("base", {}))
    s["base"] = base
    sweeps.append(s)
json.dump(sweeps, sys.stdout)
PYEOF
) || err "failed to parse config: ${CONFIG}"

NUM_SWEEPS=$(echo "$SWEEP_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
log "loaded ${NUM_SWEEPS} sweeps from ${CONFIG}"
log "batch output: ${OUTPUT_ROOT}"

# --------------------------------------------------------------------------
# Helpers for editing files in place and restoring them afterward
# --------------------------------------------------------------------------

# Back up a file before editing. Skips if already backed up (idempotent).
backup() {
  local f="$1"
  [[ -f "${f}.sweep-backup" ]] || cp "$f" "${f}.sweep-backup"
}

# Restore a file from its backup. No-op if no backup exists.
restore() {
  local f="$1"
  if [[ -f "${f}.sweep-backup" ]]; then
    mv "${f}.sweep-backup" "$f"
  fi
}

# Edit a YAML or JSON file: set a dotted field path to a value.
# Uses a small Python helper so we don't need jq/yq.
set_field() {
  local file="$1" field="$2" value="$3"
  python3 - "$file" "$field" "$value" <<'PYEOF'
import sys, json, re
from pathlib import Path

fpath, field, raw_value = sys.argv[1], sys.argv[2], sys.argv[3]
path = Path(fpath)
text = path.read_text()

# Decide the file format from the extension.
if fpath.endswith(".json"):
    doc = json.loads(text)
    # Walk the dotted field path and set the value.
    keys = field.split(".")
    obj = doc
    for k in keys[:-1]:
        obj = obj[k]
    # Auto-type the value: try int, then float, then string.
    try:
        v = int(raw_value)
    except ValueError:
        try:
            v = float(raw_value)
        except ValueError:
            v = raw_value
    obj[keys[-1]] = v
    path.write_text(json.dumps(doc, indent=2) + "\n")
else:
    # YAML: use regex replacement for the leaf key.
    # This is simpler and more robust than round-tripping through pyyaml
    # (which strips comments). We match "key: old_value" and replace
    # the value part. For nested keys like outlier_detection.interval
    # we match on the LAST component (the leaf).
    #
    # When the leaf key appears multiple times (e.g. "value:" in an
    # EnvoyFilter that also has "patch.value:"), we replace the LAST
    # occurrence — the most deeply nested one is typically the target.
    leaf = field.split(".")[-1]
    pattern = re.compile(
        r'^(\s*' + re.escape(leaf) + r'\s*:\s*)(\S+)(.*)',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        print(f"WARNING: field '{leaf}' not found in {fpath}", file=sys.stderr)
    else:
        m = matches[-1]  # last (most deeply nested) match
        new_text = text[:m.start(2)] + raw_value + text[m.end(2):]
        path.write_text(new_text)
PYEOF
}

# --------------------------------------------------------------------------
# Resolve file paths for each parameter location type
# --------------------------------------------------------------------------

resolve_file() {
  local location="$1" policy="$2" profile="$3"
  case "$location" in
    client_profile)
      echo "${PROTO_DIR}/clients/online-boutique/profiles/${profile}.json"
      ;;
    service_retries)
      echo "${PROTO_DIR}/manifests/online-boutique/service-retries.yaml"
      ;;
    policy_yaml)
      echo "${PROTO_DIR}/manifests/online-boutique/policies/${policy}.yaml"
      ;;
    fault_yaml)
      # Edit the fault manifest directly. The fault_manifest field in the
      # sweep base config names the manifest (e.g. "cartservice-100pct").
      echo "${PROTO_DIR}/manifests/online-boutique/faults/${FAULT_MANIFEST}.yaml"
      ;;
    cli)
      echo ""  # no file to edit — value goes on the command line
      ;;
    *)
      err "unknown parameter location: ${location}"
      ;;
  esac
}

# --------------------------------------------------------------------------
# Main loop: iterate over sweeps, then over values within each sweep
# --------------------------------------------------------------------------

echo "$SWEEP_JSON" | python3 -c "
import json, sys
sweeps = json.load(sys.stdin)
for s in sweeps:
    # Emit one line per sweep: tab-separated fields for bash to read.
    policies = ','.join(s['policies'])
    values = ','.join(str(v) for v in s['parameter']['values'])
    base = s['base']
    print('\t'.join([
        s['name'],
        s.get('description', ''),
        policies,
        s['parameter']['name'],
        s['parameter']['location'],
        s['parameter']['field'],
        values,
        base.get('client_profile', '') or '_NONE_',
        base.get('cpu_stress_target', '') or '_NONE_',
        str(base.get('cpu_stress_load', 95)),
        str(base.get('cpu_stress_workers', 4)),
        str(base.get('warmup', 30)),
        str(base.get('prefault', 30)),
        str(base.get('fault', 10)),
        str(base.get('recovery', 60)),
        str(base.get('cooldown', 10)),
        base.get('fault_manifest', '') or '_NONE_',
    ]))
" | while IFS=$'\t' read -r \
    SWEEP_NAME SWEEP_DESC POLICIES PARAM_NAME PARAM_LOC PARAM_FIELD \
    VALUES_CSV PROFILE CPU_TARGET CPU_LOAD CPU_WORKERS \
    WARMUP PREFAULT FAULT RECOVERY COOLDOWN FAULT_MANIFEST; do

  # Replace _NONE_ sentinels with actual empty strings. These are used
  # because bash's `read` collapses consecutive tab delimiters, so truly
  # empty fields cause all subsequent fields to shift left.
  [[ "$PROFILE" == "_NONE_" ]] && PROFILE=""
  [[ "$CPU_TARGET" == "_NONE_" ]] && CPU_TARGET=""
  [[ "$FAULT_MANIFEST" == "_NONE_" ]] && FAULT_MANIFEST=""

  # Skip sweeps not in the selection (if any were specified).
  if (( ${#SELECTED_SWEEPS[@]} > 0 )); then
    found=false
    for sel in "${SELECTED_SWEEPS[@]}"; do
      [[ "$sel" == "$SWEEP_NAME" ]] && found=true && break
    done
    $found || continue
  fi

  log "========================================"
  log "SWEEP: ${SWEEP_NAME}"
  log "  ${SWEEP_DESC}"
  log "  policies: ${POLICIES}"
  log "  parameter: ${PARAM_NAME} = [${VALUES_CSV}]"
  log "  location: ${PARAM_LOC} → ${PARAM_FIELD}"
  log "========================================"

  # Output: <output_root>/<profile>/<sweep_name>/<value>/
  # The profile directory groups all sweeps that share a client profile,
  # so the directory tree reads naturally: rps_sweep/20260412/post-cart-stress-open/1000/
  SWEEP_DIR="${OUTPUT_ROOT}/${PROFILE}/${SWEEP_NAME}"
  mkdir -p "${SWEEP_DIR}"

  # Split policies and values into arrays.
  IFS=',' read -r -a POLICIES_ARR <<< "$POLICIES"
  IFS=',' read -r -a VALUES_ARR <<< "$VALUES_CSV"

  # Determine which file to edit (if any). For policy_yaml, we need to
  # do it per-policy since each policy has its own file. For other
  # locations, the file is shared.
  FILES_TO_RESTORE=()

  for VALUE in "${VALUES_ARR[@]}"; do
    VALUE_LABEL="${VALUE}"
    # Sanitize the value for use as a directory name (e.g. "500ms" is fine,
    # "0.1" is fine, but just in case).
    VALUE_DIR="${SWEEP_DIR}/${VALUE_LABEL}"
    mkdir -p "${VALUE_DIR}"

    log "--- ${PARAM_NAME}=${VALUE} ---"

    # ---- Edit the parameter in the target file ----
    if [[ "$PARAM_LOC" != "cli" ]]; then
      if [[ "$PARAM_LOC" == "policy_yaml" ]]; then
        # Policy-specific file: edit each policy's manifest.
        for pol in "${POLICIES_ARR[@]}"; do
          TARGET_FILE=$(resolve_file "$PARAM_LOC" "$pol" "$PROFILE")
          backup "$TARGET_FILE"
          FILES_TO_RESTORE+=("$TARGET_FILE")
          set_field "$TARGET_FILE" "$PARAM_FIELD" "$VALUE"
          log "  set ${TARGET_FILE##*/} → ${PARAM_FIELD}=${VALUE}"
        done
      else
        # Shared file (client_profile or service_retries).
        TARGET_FILE=$(resolve_file "$PARAM_LOC" "" "$PROFILE")
        backup "$TARGET_FILE"
        FILES_TO_RESTORE+=("$TARGET_FILE")
        set_field "$TARGET_FILE" "$PARAM_FIELD" "$VALUE"
        log "  set ${TARGET_FILE##*/} → ${PARAM_FIELD}=${VALUE}"
      fi
    fi

    # ---- Build the run-experiment.sh command ----
    CMD=("${SCRIPT_DIR}/run-experiment.sh")
    CMD+=(--policies "$(IFS=,; echo "${POLICIES_ARR[*]}")")
    CMD+=(--client-profiles "$PROFILE")
    CMD+=(--warmup "$WARMUP" --prefault "$PREFAULT" --recovery "$RECOVERY" --cooldown "$COOLDOWN")
    CMD+=(-o "$VALUE_DIR")

    # Fault config: CPU stress, Istio fault manifest (-F), or bare fault duration.
    local_fault="$FAULT"
    if [[ "$PARAM_LOC" == "cli" && "$PARAM_FIELD" == "fault" ]]; then
      local_fault="$VALUE"
    fi
    if [[ -n "$CPU_TARGET" ]]; then
      local_load="$CPU_LOAD"
      if [[ "$PARAM_LOC" == "cli" && "$PARAM_FIELD" == "cpu_stress_load" ]]; then
        local_load="$VALUE"
      fi
      CMD+=(--cpu-stress-target "$CPU_TARGET")
      CMD+=(--cpu-stress-load "$local_load")
      CMD+=(--cpu-stress-workers "$CPU_WORKERS")
      CMD+=(--fault "$local_fault")
    elif [[ -n "$FAULT_MANIFEST" ]]; then
      CMD+=(-F "$FAULT_MANIFEST")
      CMD+=(--fault "$local_fault")
    else
      CMD+=(--fault "$local_fault")
    fi

    log "  running: ${CMD[*]}"
    # Redirect stdin from /dev/null so child processes don't consume the
    # piped sweep data that the while-read loop is iterating over.
    "${CMD[@]}" </dev/null || warn "run failed for ${SWEEP_NAME}/${VALUE_LABEL}"

    # ---- Restore edited files to their original state ----
    if (( ${#FILES_TO_RESTORE[@]} > 0 )); then
      for f in "${FILES_TO_RESTORE[@]}"; do
        restore "$f"
      done
    fi
    FILES_TO_RESTORE=()

    log "  done: ${VALUE_DIR}"
  done

  log "SWEEP ${SWEEP_NAME} complete → ${SWEEP_DIR}/"
done

log "all selected sweeps complete"
