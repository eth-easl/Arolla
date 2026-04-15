#!/usr/bin/env bash
# ==========================================================================
# run_grid.sh — run cartesian-product (grid) parameter sweeps
# ==========================================================================
#
# Sister script to run_sweep.sh. Where run_sweep.sh does OPAT (one parameter
# varied at a time), this script does full cartesian product over N
# parameters. Useful for sensitivity analysis where parameter interactions
# matter (e.g. does Arolla's r-sensitivity depend on the capacity C?).
#
# YAML schema:
#
#   - name: arolla-grid
#     policies: [arolla]
#     parameters:                  # LIST — one entry per dimension
#       - name: r
#         location: policy_yaml
#         field: pluginConfig.r
#         values: [0.01, 0.05, 0.1, 0.5, 1.0]
#       - name: t
#         location: policy_yaml
#         field: pluginConfig.t
#         values: [0.0, 0.5, 1.0, 2.0, 10.0]
#       - name: capacity
#         location: policy_yaml
#         field: pluginConfig.capacity
#         values: [1, 5, 20, 50, 100]
#     base:
#       <<: *defaults
#
# Each run's directory is named with all param=value pairs so the plotting
# script can parse them back:
#
#   outputs/prototype/<config-name>/<ts>/<profile>/<sweep-name>/r=0.01_t=0.0_capacity=5/
#
# Usage:
#   ./run_grid.sh sweeps/arolla-grid.yaml                # all grids
#   ./run_grid.sh sweeps/arolla-grid.yaml arolla-grid    # one grid
#
# ==========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"

CONFIG="${1:?usage: $0 <grid-config.yaml> [sweep-name ...]}"
shift
SELECTED_SWEEPS=("$@")

SWEEP_STEM="$(basename "${CONFIG}" .yaml)"
BATCH_TS="$(date +%Y%m%d_%H%M%S)"
OUTPUT_ROOT="${REPO_ROOT}/outputs/prototype/${SWEEP_STEM}/${BATCH_TS}"

log()  { printf '\033[1;34m[grid]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[grid]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[grid]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# File editing helpers — identical semantics to run_sweep.sh's set_field.
# --------------------------------------------------------------------------

backup() {
  local f="$1"
  [[ -f "${f}.sweep-backup" ]] || cp "$f" "${f}.sweep-backup"
}

restore() {
  local f="$1"
  if [[ -f "${f}.sweep-backup" ]]; then
    mv "${f}.sweep-backup" "$f"
  fi
}

set_field() {
  local file="$1" field="$2" value="$3"
  python3 - "$file" "$field" "$value" <<'PYEOF'
import sys, json, re
from pathlib import Path

fpath, field, raw_value = sys.argv[1], sys.argv[2], sys.argv[3]
path = Path(fpath)
text = path.read_text()

if fpath.endswith(".json"):
    doc = json.loads(text)
    keys = field.split(".")
    obj = doc
    for k in keys[:-1]:
        obj = obj[k]
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
    # YAML: replace the LAST occurrence of the leaf key (most deeply nested).
    leaf = field.split(".")[-1]
    pattern = re.compile(
        r'^(\s*' + re.escape(leaf) + r'\s*:\s*)(\S+)(.*)',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        sys.exit(f"ERROR: field '{leaf}' not found in {fpath} — aborting so we don't run with stale config")
    m = matches[-1]
    new_text = text[:m.start(2)] + raw_value + text[m.end(2):]
    path.write_text(new_text)
PYEOF
}

resolve_file() {
  local location="$1" policy="$2" profile="$3"
  case "$location" in
    client_profile)
      echo "${PROTO_DIR}/clients/online-boutique/profiles/${profile}.json" ;;
    service_retries)
      echo "${PROTO_DIR}/manifests/online-boutique/service-retries.yaml" ;;
    policy_yaml)
      # arolla's pluginConfig lives in two manifests (sidecar + gateway);
      # both must be edited in lockstep or the gateway stays stale while
      # the sidecar sweeps.
      echo "${PROTO_DIR}/manifests/online-boutique/policies/${policy}.yaml"
      [[ "${policy}" == "arolla" ]] && \
        echo "${PROTO_DIR}/manifests/online-boutique/policies/arolla-gateway.yaml"
      ;;
    *)
      err "unknown parameter location: ${location}" ;;
  esac
}

# --------------------------------------------------------------------------
# Parse the YAML config. For each grid sweep, expand the cartesian product
# of parameter values into a list of "combinations". Emit ONE NDJSON line
# per combination that the bash loop can consume.
# --------------------------------------------------------------------------

COMBO_NDJSON=$(python3 - "$CONFIG" <<'PYEOF'
import yaml, json, sys, itertools
config = yaml.safe_load(open(sys.argv[1]))
defaults = config.get("defaults", {})
for s in config["sweeps"]:
    params = s.get("parameters")
    if not params:
        print(f"ERROR: sweep {s.get('name')} missing 'parameters:' list",
              file=sys.stderr)
        sys.exit(2)
    base = dict(defaults)
    base.update(s.get("base", {}))
    value_lists = [p["values"] for p in params]
    names       = [p["name"] for p in params]
    locations   = [p["location"] for p in params]
    fields      = [p["field"] for p in params]

    for combo in itertools.product(*value_lists):
        # Use "__" (double underscore) as the pair separator so single
        # underscores inside parameter names (e.g. base_ejection_time)
        # don't confuse the parser.
        label = "__".join(f"{n}={v}" for n, v in zip(names, combo))
        rec = {
            "sweep_name":  s["name"],
            "description": s.get("description", ""),
            "policies":    s["policies"],
            "names":       names,
            "locations":   locations,
            "fields":      fields,
            "values":      [str(v) for v in combo],
            "label":       label,
            "profile":     base.get("client_profile", ""),
            "warmup":      base.get("warmup", 30),
            "prefault":    base.get("prefault", 30),
            "fault":       base.get("fault", 10),
            "recovery":    base.get("recovery", 60),
            "cooldown":    base.get("cooldown", 10),
            "cpu_stress_target":  base.get("cpu_stress_target", ""),
            "cpu_stress_load":    base.get("cpu_stress_load", 95),
            "cpu_stress_workers": base.get("cpu_stress_workers", 4),
            "fault_manifest":     base.get("fault_manifest", ""),
        }
        print(json.dumps(rec))
PYEOF
) || err "failed to parse config: ${CONFIG}"

NUM_COMBOS=$(echo "$COMBO_NDJSON" | wc -l | tr -d ' ')
log "expanded ${NUM_COMBOS} grid runs from ${CONFIG}"
log "batch output: ${OUTPUT_ROOT}"

# Warn on leftover backups from a previous interrupted run. If present, they
# will be treated as the source of truth by backup() and silently mask any
# manual edits the user made to policy/profile files in the meantime.
mapfile -t STALE_BACKUPS < <(find "${PROTO_DIR}" -name '*.sweep-backup' 2>/dev/null)
if (( ${#STALE_BACKUPS[@]} > 0 )); then
  warn "found ${#STALE_BACKUPS[@]} stale .sweep-backup file(s) from a prior interrupted run:"
  for f in "${STALE_BACKUPS[@]}"; do warn "  ${f}"; done
  warn "these will be used as the 'clean' baseline for restore. If you want to"
  warn "use the current file contents as the baseline, delete them and re-run."
fi

# --------------------------------------------------------------------------
# Main loop: iterate over combinations. Each iteration edits all parameter
# fields, runs one experiment, and restores the files.
# --------------------------------------------------------------------------

prev_sweep=""
FAILED_CELLS=()   # parallel arrays: FAILED_CELLS[i] → label, FAILED_REASONS[i] → short reason
FAILED_REASONS=()

while IFS= read -r record; do
  SWEEP_NAME=$(echo "$record" | python3 -c "import json,sys; print(json.load(sys.stdin)['sweep_name'])")

  # Skip if not in the selection.
  if (( ${#SELECTED_SWEEPS[@]} > 0 )); then
    found=false
    for sel in "${SELECTED_SWEEPS[@]}"; do
      [[ "$sel" == "$SWEEP_NAME" ]] && found=true && break
    done
    $found || continue
  fi

  # Extract all fields from the JSON record via Python.
  eval "$(echo "$record" | python3 -c "
import json, sys, shlex
r = json.load(sys.stdin)
print(f'SWEEP_DESC={shlex.quote(r[\"description\"])}')
print(f'POLICIES={shlex.quote(\",\".join(r[\"policies\"]))}')
print(f'NAMES={shlex.quote(\",\".join(r[\"names\"]))}')
print(f'LOCATIONS={shlex.quote(\",\".join(r[\"locations\"]))}')
print(f'FIELDS={shlex.quote(\",\".join(r[\"fields\"]))}')
print(f'VALUES={shlex.quote(\",\".join(r[\"values\"]))}')
print(f'LABEL={shlex.quote(r[\"label\"])}')
print(f'PROFILE={shlex.quote(r[\"profile\"])}')
print(f'WARMUP={shlex.quote(str(r[\"warmup\"]))}')
print(f'PREFAULT={shlex.quote(str(r[\"prefault\"]))}')
print(f'FAULT={shlex.quote(str(r[\"fault\"]))}')
print(f'RECOVERY={shlex.quote(str(r[\"recovery\"]))}')
print(f'COOLDOWN={shlex.quote(str(r[\"cooldown\"]))}')
print(f'CPU_TARGET={shlex.quote(r[\"cpu_stress_target\"])}')
print(f'CPU_LOAD={shlex.quote(str(r[\"cpu_stress_load\"]))}')
print(f'CPU_WORKERS={shlex.quote(str(r[\"cpu_stress_workers\"]))}')
print(f'FAULT_MANIFEST={shlex.quote(r[\"fault_manifest\"])}')
")"

  # Per-sweep banner (only when we cross into a new sweep).
  if [[ "$SWEEP_NAME" != "$prev_sweep" ]]; then
    log "========================================"
    log "GRID: ${SWEEP_NAME}"
    log "  ${SWEEP_DESC}"
    log "  policies: ${POLICIES}"
    log "  parameters: ${NAMES}"
    log "  fields:     ${FIELDS}"
    log "========================================"
    prev_sweep="$SWEEP_NAME"
  fi

  IFS=',' read -r -a POLICIES_ARR <<< "$POLICIES"
  IFS=',' read -r -a LOCATIONS_ARR <<< "$LOCATIONS"
  IFS=',' read -r -a FIELDS_ARR <<< "$FIELDS"
  IFS=',' read -r -a VALUES_ARR <<< "$VALUES"

  SWEEP_DIR="${OUTPUT_ROOT}/${PROFILE}/${SWEEP_NAME}"
  VALUE_DIR="${SWEEP_DIR}/${LABEL}"
  mkdir -p "${VALUE_DIR}"

  log "--- ${LABEL} ---"

  # Edit each parameter. Accumulate files to restore after the run.
  FILES_TO_RESTORE=()
  for ((i=0; i<${#FIELDS_ARR[@]}; i++)); do
    loc="${LOCATIONS_ARR[i]}"
    field="${FIELDS_ARR[i]}"
    value="${VALUES_ARR[i]}"

    if [[ "$loc" == "policy_yaml" ]]; then
      for pol in "${POLICIES_ARR[@]}"; do
        # resolve_file may emit multiple paths (e.g. arolla → sidecar +
        # gateway manifests) — edit each so pluginConfig stays in sync.
        while IFS= read -r target; do
          [[ -z "$target" ]] && continue
          backup "$target"
          FILES_TO_RESTORE+=("$target")
          set_field "$target" "$field" "$value"
          log "  set ${target##*/} → ${field}=${value}"
        done < <(resolve_file "$loc" "$pol" "$PROFILE")
      done
    elif [[ "$loc" == "cli" ]]; then
      err "location: cli is not implemented — parameter '${field}' would be silently ignored"
    else
      target=$(resolve_file "$loc" "" "$PROFILE")
      backup "$target"
      FILES_TO_RESTORE+=("$target")
      set_field "$target" "$field" "$value"
      log "  set ${target##*/} → ${field}=${value}"
    fi
  done

  # Build the run-experiment.sh command.
  CMD=("${SCRIPT_DIR}/run-experiment.sh")
  CMD+=(--policies "$(IFS=,; echo "${POLICIES_ARR[*]}")")
  CMD+=(--client-profiles "$PROFILE")
  CMD+=(--warmup "$WARMUP" --prefault "$PREFAULT" --recovery "$RECOVERY" --cooldown "$COOLDOWN")
  CMD+=(-o "$VALUE_DIR")

  if [[ -n "$CPU_TARGET" ]]; then
    CMD+=(--cpu-stress-target "$CPU_TARGET")
    CMD+=(--cpu-stress-load "$CPU_LOAD")
    CMD+=(--cpu-stress-workers "$CPU_WORKERS")
    CMD+=(--fault "$FAULT")
  elif [[ -n "$FAULT_MANIFEST" ]]; then
    CMD+=(-F "$FAULT_MANIFEST")
    CMD+=(--fault "$FAULT")
  else
    CMD+=(--fault "$FAULT")
  fi

  log "  running: ${CMD[*]}"
  cell_ok=true
  "${CMD[@]}" </dev/null >"${VALUE_DIR}/run.log" 2>&1 || cell_ok=false

  # Classify failure: verify-mismatch (policy values wrong) vs other.
  if ! $cell_ok; then
    reason="run failed"
    if grep -q '^\[verify\] FAILED' "${VALUE_DIR}/run.log" 2>/dev/null; then
      # Pull the first ✗ line from the verify output for a concrete hint.
      mismatch=$(grep -m1 '✗' "${VALUE_DIR}/run.log" 2>/dev/null || true)
      reason="policy values NOT applied: ${mismatch#*✗ }"
    fi
    warn "${SWEEP_NAME}/${LABEL} — ${reason}"
    warn "  see ${VALUE_DIR}/run.log"
    FAILED_CELLS+=("${SWEEP_NAME}/${LABEL}")
    FAILED_REASONS+=("${reason}")
  fi

  # Restore edited files. Dedup first — same policy_yaml may appear N times
  # when multiple grid parameters edit the same file.
  declare -A seen=()
  for f in "${FILES_TO_RESTORE[@]}"; do
    [[ -n "${seen[$f]:-}" ]] && continue
    seen[$f]=1
    restore "$f"
  done
  unset seen

  log "  done: ${VALUE_DIR}"
done <<< "$COMBO_NDJSON"

# --------------------------------------------------------------------------
# Final summary — list every cell that did NOT produce a clean run, with
# the reason (policy-mismatch vs other). This is what the user reads to
# know which data files to trust.
# --------------------------------------------------------------------------
echo >&2
if (( ${#FAILED_CELLS[@]} == 0 )); then
  log "all ${NUM_COMBOS} grid cells completed successfully"
else
  warn "========================================================"
  warn "FAILED CELLS: ${#FAILED_CELLS[@]} / ${NUM_COMBOS}"
  warn "========================================================"
  for i in "${!FAILED_CELLS[@]}"; do
    warn "  ${FAILED_CELLS[i]}"
    warn "      → ${FAILED_REASONS[i]}"
  done
  warn "These cells' data should NOT be trusted. Inspect run.log and re-run."
fi

log "batch output: ${OUTPUT_ROOT}"
