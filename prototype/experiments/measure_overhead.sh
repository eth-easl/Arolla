#!/usr/bin/env bash
# ==========================================================================
# measure_overhead.sh — measure CPU/memory overhead with and without Arolla
# ==========================================================================
#
# Runs N rounds of steady-state load at a given RPS, captures kubectl top
# for each policy, and reports mean ± stddev for CPU and memory.
#
# Usage:
#   ./measure_overhead.sh [options]
#
# Options:
#   --rps <N>          Offered load (default: 1500)
#   --rounds <N>       Number of measurement rounds (default: 3)
#   --warmup <sec>     Seconds of load before capturing (default: 40)
#   --policies <list>  Comma-separated policies (default: no-control,arolla)
#   --output <dir>     Output directory (default: outputs/prototype/overhead)
#
# Output:
#   <output>/summary.csv         — per-round per-policy per-service metrics
#   <output>/report.txt          — human-readable mean ± stddev
#
# ==========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"

source "${PROTO_DIR}/k8s-config.sh"

# Defaults
RPS=1500
ROUNDS=3
WARMUP_SEC=40
POLICIES="no-control,arolla"
OUTPUT_DIR="${REPO_ROOT}/outputs/prototype/overhead/$(date +%Y%m%d_%H%M%S)"

log()  { printf '\033[1;34m[overhead]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[overhead]\033[0m %s\n' "$*" >&2; }

# Parse CLI
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rps)       RPS="$2"; shift 2 ;;
    --rounds)    ROUNDS="$2"; shift 2 ;;
    --warmup)    WARMUP_SEC="$2"; shift 2 ;;
    --policies)  POLICIES="$2"; shift 2 ;;
    --output)    OUTPUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

IFS=',' read -r -a POLICY_ARR <<< "$POLICIES"
mkdir -p "$OUTPUT_DIR"

SUMMARY="${OUTPUT_DIR}/summary.csv"
echo "round,policy,service,container,cpu_m,mem_mi" > "$SUMMARY"

log "Configuration:"
log "  RPS:      $RPS"
log "  Rounds:   $ROUNDS"
log "  Warmup:   ${WARMUP_SEC}s"
log "  Policies: ${POLICIES}"
log "  Output:   ${OUTPUT_DIR}"
log ""

capture_metrics() {
  local round="$1" policy="$2"
  kubectl top pods -n online-boutique --containers --no-headers 2>/dev/null | \
    grep istio-proxy | while read -r pod container cpu mem rest; do
      # Extract service name from pod name (strip hash suffix)
      svc=$(echo "$pod" | sed 's/-[a-z0-9]*-[a-z0-9]*$//')
      # Extract numeric values
      cpu_val="${cpu%m}"
      mem_val="${mem%Mi}"
      echo "${round},${policy},${svc},${container},${cpu_val},${mem_val}" >> "$SUMMARY"
    done
}

for round in $(seq 1 "$ROUNDS"); do
  for policy in "${POLICY_ARR[@]}"; do
    log "=== Round ${round}/${ROUNDS}, policy: ${policy} ==="

    # Restart pods for clean state
    log "  restarting pods..."
    kubectl rollout restart deployment -n online-boutique >/dev/null 2>&1 || true
    kubectl rollout status deployment -n online-boutique --timeout=120s 2>/dev/null || \
      warn "  rollout timed out"

    # Switch policy
    log "  switching to ${policy}..."
    "${PROTO_DIR}/deploy-policy.sh" switch "$policy" >/dev/null 2>&1
    sleep 15  # xDS settle

    # Start load
    log "  starting ${RPS} rps load..."
    # Temporarily set rate_rps in profile
    PROFILE_FILE="${PROTO_DIR}/clients/online-boutique/profiles/post-cart-stress-open.json"
    cp "$PROFILE_FILE" "${PROFILE_FILE}.overhead-backup"
    python3 -c "
import json
f = '${PROFILE_FILE}'
d = json.loads(open(f).read())
d['rate_rps'] = ${RPS}
open(f, 'w').write(json.dumps(d, indent=2) + '\n')
"
    PROFILES=post-cart-stress-open \
      "${PROTO_DIR}/clients/online-boutique/run-clients.sh" start >/dev/null 2>&1

    log "  waiting ${WARMUP_SEC}s for steady state..."
    sleep "$WARMUP_SEC"

    # Capture metrics
    log "  capturing kubectl top..."
    capture_metrics "$round" "$policy"

    # Stop load and restore profile
    "${PROTO_DIR}/clients/online-boutique/run-clients.sh" stop >/dev/null 2>&1
    mv "${PROFILE_FILE}.overhead-backup" "$PROFILE_FILE"

    log "  done"
    echo ""
  done
done

# Generate report
log "Generating report..."
python3 - "$SUMMARY" "$OUTPUT_DIR/report.txt" <<'PYEOF'
import sys, csv
from collections import defaultdict
import statistics

summary_file, report_file = sys.argv[1], sys.argv[2]

# Collect per-(policy, service) lists of cpu and mem
data = defaultdict(lambda: {"cpu": [], "mem": []})
with open(summary_file) as f:
    reader = csv.DictReader(f)
    for row in reader:
        key = (row["policy"], row["service"])
        data[key]["cpu"].append(float(row["cpu_m"]))
        data[key]["mem"].append(float(row["mem_mi"]))

# Also aggregate per-policy (avg across services per round)
policy_data = defaultdict(lambda: {"cpu": [], "mem": []})
for (policy, svc), vals in data.items():
    for c in vals["cpu"]:
        policy_data[policy]["cpu"].append(c)
    for m in vals["mem"]:
        policy_data[policy]["mem"].append(m)

lines = []
lines.append("=" * 60)
lines.append("Overhead Measurement Report")
lines.append("=" * 60)

# Per-policy summary
lines.append("\nPer-policy averages (across all services and rounds):")
lines.append(f"{'Policy':<22} {'CPU (m)':>12} {'Memory (Mi)':>14}")
lines.append("-" * 50)
for policy in sorted(policy_data.keys()):
    cpus = policy_data[policy]["cpu"]
    mems = policy_data[policy]["mem"]
    cpu_mean = statistics.mean(cpus)
    cpu_std = statistics.stdev(cpus) if len(cpus) > 1 else 0
    mem_mean = statistics.mean(mems)
    mem_std = statistics.stdev(mems) if len(mems) > 1 else 0
    lines.append(f"{policy:<22} {cpu_mean:>6.0f} ± {cpu_std:<4.0f} {mem_mean:>7.0f} ± {mem_std:<4.0f}")

# Hot services only (frontend, cart, productcatalog, gateway)
hot_services = ["frontend", "cartservice", "productcatalogservice",
                "boutique-gateway-istio", "redis-cart"]
lines.append("\nHot-path services only:")
lines.append(f"{'Policy':<22} {'Service':<28} {'CPU (m)':>12} {'Memory (Mi)':>14}")
lines.append("-" * 78)
for policy in sorted(policy_data.keys()):
    for svc in hot_services:
        key = (policy, svc)
        if key not in data:
            continue
        cpus = data[key]["cpu"]
        mems = data[key]["mem"]
        cpu_mean = statistics.mean(cpus)
        cpu_std = statistics.stdev(cpus) if len(cpus) > 1 else 0
        mem_mean = statistics.mean(mems)
        mem_std = statistics.stdev(mems) if len(mems) > 1 else 0
        lines.append(f"{policy:<22} {svc:<28} {cpu_mean:>5.0f} ± {cpu_std:<4.0f} {mem_mean:>6.0f} ± {mem_std:<4.0f}")

report = "\n".join(lines) + "\n"
print(report)
with open(report_file, "w") as f:
    f.write(report)
print(f"wrote {report_file}")
PYEOF

log "All done → ${OUTPUT_DIR}/"
