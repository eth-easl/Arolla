#!/usr/bin/env bash
# ==========================================================================
# run_sensitivity.sh — run parameter sweeps defined in a YAML config
# ==========================================================================
#
# Reads a sweep config (e.g. sweeps/sensitivity.yaml) and for each sweep
# listed, iterates over the parameter values, edits the target file in
# place, runs run-experiment.sh, and restores the original file.
#
# Usage:
#   ./run_sensitivity.sh sweeps/sensitivity.yaml                # all sweeps
#   ./run_sensitivity.sh sweeps/sensitivity.yaml fault-duration  # one sweep
#   ./run_sensitivity.sh sweeps/sensitivity.yaml arolla-time-refill arolla-event-refill
#
# The second+ arguments are sweep names to run. If omitted, ALL sweeps
# in the config are run sequentially.
#
# Output goes to:
#   outputs/prototype/sensitivity/<timestamp>/<sweep-name>/<param-value>/
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

BATCH_TS="$(date +%Y%m%d_%H%M%S)"
OUTPUT_ROOT="${REPO_ROOT}/outputs/prototype/sensitivity/${BATCH_TS}"

log()  { printf '\033[1;34m[sensitivity]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[sensitivity]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[sensitivity]\033[0m %s\n' "$*" >&2; exit 1; }

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
    leaf = field.split(".")[-1]
    # Match the leaf key followed by a colon and a value, preserving
    # any inline comment. Replace only the value portion.
    pattern = re.compile(
        r'^(\s*' + re.escape(leaf) + r'\s*:\s*)(\S+)(.*)',
        re.MULTILINE,
    )
    new_text, n = pattern.subn(r'\g<1>' + raw_value + r'\g<3>', text, count=1)
    if n == 0:
        print(f"WARNING: field '{leaf}' not found in {fpath}", file=sys.stderr)
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
        base.get('client_profile', ''),
        base.get('cpu_stress_target', ''),
        str(base.get('cpu_stress_load', 95)),
        str(base.get('cpu_stress_workers', 4)),
        str(base.get('warmup', 30)),
        str(base.get('prefault', 30)),
        str(base.get('fault', 10)),
        str(base.get('recovery', 60)),
        str(base.get('cooldown', 10)),
    ]))
" | while IFS=$'\t' read -r \
    SWEEP_NAME SWEEP_DESC POLICIES PARAM_NAME PARAM_LOC PARAM_FIELD \
    VALUES_CSV PROFILE CPU_TARGET CPU_LOAD CPU_WORKERS \
    WARMUP PREFAULT FAULT RECOVERY COOLDOWN; do

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

  SWEEP_DIR="${OUTPUT_ROOT}/${SWEEP_NAME}"
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

    # Fault config: either CPU stress or Istio fault manifest.
    if [[ -n "$CPU_TARGET" ]]; then
      local_fault="$FAULT"
      local_load="$CPU_LOAD"
      # Override from CLI-location sweep values.
      if [[ "$PARAM_LOC" == "cli" ]]; then
        case "$PARAM_FIELD" in
          fault)           local_fault="$VALUE" ;;
          cpu_stress_load) local_load="$VALUE"  ;;
        esac
      fi
      CMD+=(--cpu-stress-target "$CPU_TARGET")
      CMD+=(--cpu-stress-load "$local_load")
      CMD+=(--cpu-stress-workers "$CPU_WORKERS")
      CMD+=(--fault "$local_fault")
    else
      CMD+=(--fault "$FAULT")
    fi

    log "  running: ${CMD[*]}"
    "${CMD[@]}" || warn "run failed for ${SWEEP_NAME}/${VALUE_LABEL}"

    # ---- Restore edited files to their original state ----
    for f in "${FILES_TO_RESTORE[@]}"; do
      restore "$f"
    done
    FILES_TO_RESTORE=()

    log "  done: ${VALUE_DIR}"
  done

  log "SWEEP ${SWEEP_NAME} complete → ${SWEEP_DIR}/"
done

log "all selected sweeps complete"
