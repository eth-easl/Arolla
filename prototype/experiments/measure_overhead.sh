#!/usr/bin/env bash
# ==========================================================================
# measure_overhead.sh — sidecar CPU/memory + client latency overhead
# ==========================================================================
#
# For each (RPS, policy) pair we run ROUNDS independent measurement runs.
# Each run restarts the deployments, switches the policy, starts the
# loadgen at the target RPS, warms up, then samples both `kubectl top`
# (at SAMPLE_INTERVAL cadence) and client request CSVs over a
# MEASURE_SEC measurement window. Results are time-filtered to the
# measurement window so warmup transients don't bias the mean.
#
# Usage:
#   ./measure_overhead.sh [options]
#
# Options:
#   --rps <list>         Comma-separated RPS values (default: 600)
#   --rounds <N>         Independent runs per (RPS, policy) (default: 5)
#   --warmup <sec>       Warmup before measurement (default: 60)
#   --measurement <sec>  Measurement window (default: 90)
#   --sample-interval <sec>
#                        kubectl top cadence within window (default: 15)
#   --policies <list>    Comma-separated policies (default: no-control,arolla)
#   --output <dir>       Output directory (default: outputs/prototype/overhead/<ts>)
#
# Output:
#   <output>/sidecar.csv   — per-sample sidecar CPU/mem
#   <output>/client.csv    — per-run client latency + actual RPS
#   <output>/report.txt    — human-readable summary
#
# ==========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PROTO_DIR}/.." && pwd)"

source "${PROTO_DIR}/k8s-config.sh"

REMOTE_BASE="${REMOTE_BASE:-/tmp/online-boutique-clients}"
REMOTE_METRICS_DIR="${REMOTE_BASE}/metrics"

# Defaults
RPS_LIST="600"
ROUNDS=3
WARMUP_SEC=60
MEASURE_SEC=90
SAMPLE_INTERVAL=15
POLICIES="no-control,arolla"
HOT_SERVICES=("frontend" "cartservice" "boutique-gateway-istio")
OUTPUT_DIR="${REPO_ROOT}/outputs/nsdi/overhead/$(date +%Y%m%d_%H%M%S)"

log()  { printf '\033[1;34m[overhead]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[overhead]\033[0m %s\n' "$*" >&2; }

# Parse CLI
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rps)              RPS_LIST="$2"; shift 2 ;;
    --rounds)           ROUNDS="$2"; shift 2 ;;
    --warmup)           WARMUP_SEC="$2"; shift 2 ;;
    --measurement)      MEASURE_SEC="$2"; shift 2 ;;
    --sample-interval)  SAMPLE_INTERVAL="$2"; shift 2 ;;
    --policies)         POLICIES="$2"; shift 2 ;;
    --output)           OUTPUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

IFS=',' read -r -a RPS_ARR    <<< "$RPS_LIST"
IFS=',' read -r -a POLICY_ARR <<< "$POLICIES"
mkdir -p "$OUTPUT_DIR"

SIDECAR_CSV="${OUTPUT_DIR}/sidecar.csv"
CLIENT_CSV="${OUTPUT_DIR}/client.csv"
echo "rps,round,sample,policy,pod,service,container,cpu_m,mem_mi" > "$SIDECAR_CSV"
echo "rps,round,policy,window_sec,total_attempts,total_ok,success_rate,actual_rps,p50_ms,p95_ms,p99_ms" > "$CLIENT_CSV"

log "Configuration:"
log "  RPS list:        ${RPS_LIST}"
log "  Rounds:          $ROUNDS"
log "  Warmup:          ${WARMUP_SEC}s"
log "  Measurement:     ${MEASURE_SEC}s"
log "  Sample interval: ${SAMPLE_INTERVAL}s"
log "  Policies:        ${POLICIES}"
log "  Output:          ${OUTPUT_DIR}"
log ""

SSH="ssh ${SSH_OPTS} ${SSH_USER}@${CLIENT_HOST}"

# Run a kubectl top snapshot and append one row per istio-proxy container
# to SIDECAR_CSV, tagged with (rps, round, sample, policy).
capture_metrics() {
  local rps="$1" round="$2" sample="$3" policy="$4"
  kubectl top pods -n online-boutique --containers --no-headers 2>/dev/null | \
    grep istio-proxy | while read -r pod container cpu mem rest; do
      svc=$(echo "$pod" | sed 's/-[a-z0-9]*-[a-z0-9]*$//')
      cpu_val="${cpu%m}"
      mem_val="${mem%Mi}"
      echo "${rps},${round},${sample},${policy},${pod},${svc},${container},${cpu_val},${mem_val}" >> "$SIDECAR_CSV"
    done
}

# Fetch client CSVs from the loadgen host, compute latency / actual RPS
# inside [t_start, t_end], and append one row to CLIENT_CSV.
compute_client_stats() {
  local rps="$1" round="$2" policy="$3" t_start="$4" t_end="$5"
  local run_dir="${OUTPUT_DIR}/raw/rps${rps}_r${round}_${policy}"
  mkdir -p "$run_dir"
  # Match both the single-shard layout (client_attempts.csv) and the
  # multi-shard layout (client_attempts.shard*.csv). Route stderr through
  # a tee so a silent scp failure still lands in the log.
  if ! scp ${SSH_OPTS} \
        "${SSH_USER}@${CLIENT_HOST}:${REMOTE_METRICS_DIR}/client_attempts*.csv" \
        "$run_dir/" 2>&1 | tee -a "${OUTPUT_DIR}/scp.log" >/dev/null; then
    warn "  scp failed for rps=${rps} round=${round} policy=${policy}"
  fi
  # If neither filename pattern landed, give up on this run's client stats.
  if ! compgen -G "${run_dir}/client_attempts*.csv" >/dev/null; then
    warn "  no client CSVs fetched for rps=${rps} round=${round} policy=${policy}"
    return
  fi

  python3 - "$run_dir" "$CLIENT_CSV" "$rps" "$round" "$policy" "$t_start" "$t_end" <<'PYEOF'
import csv, glob, os, sys, statistics

run_dir, out_csv, rps, rnd, policy, t_start, t_end = sys.argv[1:]
t_start, t_end = float(t_start), float(t_end)
window = t_end - t_start

latencies_ms = []
total_attempts = 0
total_ok = 0

for path in sorted(glob.glob(os.path.join(run_dir, "client_attempts*.csv"))):
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = float(row["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts < t_start or ts > t_end:
                continue
            total_attempts += 1
            if str(row.get("ok", "")).lower() in ("true", "1", "t", "yes"):
                total_ok += 1
            try:
                latencies_ms.append(float(row["latency_s"]) * 1000.0)
            except (KeyError, ValueError):
                pass

if total_attempts == 0:
    sys.stderr.write(f"  [warn] no client rows in window for rps={rps} round={rnd} policy={policy}\n")
    sys.exit(0)

latencies_ms.sort()
def pct(p):
    if not latencies_ms:
        return 0.0
    k = max(0, min(len(latencies_ms) - 1, int(round(p / 100.0 * (len(latencies_ms) - 1)))))
    return latencies_ms[k]

success_rate = total_ok / total_attempts if total_attempts else 0.0
actual_rps = total_attempts / window if window > 0 else 0.0

with open(out_csv, "a") as f:
    f.write(f"{rps},{rnd},{policy},{window:.1f},{total_attempts},{total_ok},"
            f"{success_rate:.4f},{actual_rps:.1f},"
            f"{pct(50):.1f},{pct(95):.1f},{pct(99):.1f}\n")
PYEOF
}

# Set the profile's rate_rps in-place, backing up the original for
# restore. We pass the value through argv so shell quoting doesn't get
# in the way of the Python literal.
set_profile_rps() {
  local rps="$1"
  local profile_file="${PROTO_DIR}/clients/online-boutique/profiles/post-cart-stress-open.json"
  cp "$profile_file" "${profile_file}.overhead-backup"
  python3 - "$profile_file" "$rps" <<'PYEOF'
import json, sys
path, rps = sys.argv[1], int(sys.argv[2])
d = json.loads(open(path).read())
d["rate_rps"] = rps
open(path, "w").write(json.dumps(d, indent=2) + "\n")
PYEOF
}

restore_profile() {
  local profile_file="${PROTO_DIR}/clients/online-boutique/profiles/post-cart-stress-open.json"
  [[ -f "${profile_file}.overhead-backup" ]] && mv "${profile_file}.overhead-backup" "$profile_file"
}

run_single() {
  local rps="$1" round="$2" policy="$3"
  log "=== rps=${rps} round=${round}/${ROUNDS} policy=${policy} ==="

  log "  restarting pods..."
  kubectl rollout restart deployment -n online-boutique >/dev/null 2>&1 || true
  kubectl rollout status deployment -n online-boutique --timeout=120s 2>/dev/null || \
    warn "  rollout timed out"

  log "  switching to ${policy}..."
  "${PROTO_DIR}/deploy-policy.sh" switch "$policy" >/dev/null 2>&1
  sleep 15  # xDS settle

  # Clear remote CSVs so this round's data is isolated.
  ${SSH} "rm -f ${REMOTE_METRICS_DIR}/client_attempts*.csv" 2>/dev/null || true

  log "  setting profile to ${rps} rps..."
  set_profile_rps "$rps"

  log "  starting load..."
  PROFILES=post-cart-stress-open \
    "${PROTO_DIR}/clients/online-boutique/run-clients.sh" start >/dev/null 2>&1

  log "  warmup ${WARMUP_SEC}s..."
  sleep "$WARMUP_SEC"

  # Measurement window: take N samples of kubectl top at SAMPLE_INTERVAL
  # cadence. Record the actual wall-clock window boundaries so the
  # client-side parse uses the same interval.
  local t_start t_end n_samples sample
  t_start="$(date +%s)"
  n_samples=$(( MEASURE_SEC / SAMPLE_INTERVAL ))
  (( n_samples < 1 )) && n_samples=1
  log "  measurement window ${MEASURE_SEC}s (${n_samples} samples)..."
  for (( sample=1; sample<=n_samples; sample++ )); do
    capture_metrics "$rps" "$round" "$sample" "$policy"
    (( sample < n_samples )) && sleep "$SAMPLE_INTERVAL"
  done
  t_end="$(date +%s)"

  "${PROTO_DIR}/clients/online-boutique/run-clients.sh" stop >/dev/null 2>&1 || true
  restore_profile

  # Pull shards and compute client-side latency + actual throughput for
  # the measurement window only.
  compute_client_stats "$rps" "$round" "$policy" "$t_start" "$t_end"

  log "  done"
  echo ""
}

# Restore profile if user Ctrl-Cs mid-run.
trap restore_profile EXIT

for rps in "${RPS_ARR[@]}"; do
  for round in $(seq 1 "$ROUNDS"); do
    for policy in "${POLICY_ARR[@]}"; do
      run_single "$rps" "$round" "$policy"
    done
  done
done

# -------------------------------------------------------------------------
# Report
# -------------------------------------------------------------------------
log "Generating report..."
HOT_JOIN="$(IFS=,; echo "${HOT_SERVICES[*]}")"
python3 - "$SIDECAR_CSV" "$CLIENT_CSV" "$OUTPUT_DIR/report.txt" "$HOT_JOIN" <<'PYEOF'
import sys, csv, statistics
from collections import defaultdict

sidecar_csv, client_csv, report_file, hot_csv = sys.argv[1:]
HOT = [s for s in hot_csv.split(",") if s]

def fmt_pair(mean, std):
    return f"{mean:>6.0f} ± {std:<5.0f}"

# ---- sidecar ------------------------------------------------------------
# Aggregate at (rps, policy, service) — pool across rounds & samples so
# each reported number is a steady-state estimate for that service.
svc = defaultdict(lambda: {"cpu": [], "mem": []})
all_svc = defaultdict(lambda: {"cpu": [], "mem": []})  # full-sidecar avg
with open(sidecar_csv) as f:
    for row in csv.DictReader(f):
        rps = int(row["rps"])
        policy = row["policy"]
        service = row["service"]
        cpu = float(row["cpu_m"])
        mem = float(row["mem_mi"])
        svc[(rps, policy, service)]["cpu"].append(cpu)
        svc[(rps, policy, service)]["mem"].append(mem)
        all_svc[(rps, policy)]["cpu"].append(cpu)
        all_svc[(rps, policy)]["mem"].append(mem)

rps_values = sorted({k[0] for k in svc.keys()})
policies   = sorted({k[1] for k in svc.keys()})

lines = []
lines.append("=" * 80)
lines.append("Overhead Measurement Report")
lines.append("=" * 80)

# Hot-path sidecars
lines.append("\n[Sidecar CPU (millicores) / Memory (MiB) — hot-path services]")
lines.append(f"{'RPS':>6}  {'Policy':<14}  {'Service':<26}  {'CPU (m)':>14}  {'Memory (Mi)':>14}")
lines.append("-" * 82)
for rps in rps_values:
    for service in HOT:
        for policy in policies:
            vals = svc.get((rps, policy, service))
            if not vals or not vals["cpu"]:
                continue
            cpus, mems = vals["cpu"], vals["mem"]
            cpu_m = statistics.mean(cpus)
            cpu_s = statistics.stdev(cpus) if len(cpus) > 1 else 0.0
            mem_m = statistics.mean(mems)
            mem_s = statistics.stdev(mems) if len(mems) > 1 else 0.0
            lines.append(f"{rps:>6}  {policy:<14}  {service:<26}  "
                         f"{fmt_pair(cpu_m, cpu_s)}  {fmt_pair(mem_m, mem_s)}")
    lines.append("")

# Full-sidecar average (optional)
lines.append("[Sidecar averages across ALL istio-proxy pods]")
lines.append(f"{'RPS':>6}  {'Policy':<14}  {'CPU (m)':>14}  {'Memory (Mi)':>14}")
lines.append("-" * 54)
for rps in rps_values:
    for policy in policies:
        vals = all_svc.get((rps, policy))
        if not vals or not vals["cpu"]:
            continue
        cpus, mems = vals["cpu"], vals["mem"]
        cpu_m = statistics.mean(cpus)
        cpu_s = statistics.stdev(cpus) if len(cpus) > 1 else 0.0
        mem_m = statistics.mean(mems)
        mem_s = statistics.stdev(mems) if len(mems) > 1 else 0.0
        lines.append(f"{rps:>6}  {policy:<14}  "
                     f"{fmt_pair(cpu_m, cpu_s)}  {fmt_pair(mem_m, mem_s)}")

# ---- client -------------------------------------------------------------
client = defaultdict(list)  # (rps, policy) -> list of row dicts
with open(client_csv) as f:
    for row in csv.DictReader(f):
        client[(int(row["rps"]), row["policy"])].append(row)

if client:
    lines.append("\n[Client-side latency / throughput — measurement window only]")
    lines.append(f"{'RPS':>6}  {'Policy':<14}  {'Actual RPS':>12}  {'Success':>8}  "
                 f"{'p50 ms':>10}  {'p95 ms':>10}  {'p99 ms':>10}")
    lines.append("-" * 80)
    for rps in rps_values:
        for policy in policies:
            rows = client.get((rps, policy), [])
            if not rows:
                continue
            def col(name, cast=float):
                return [cast(r[name]) for r in rows if r.get(name)]
            def mean_std(xs):
                if not xs:
                    return 0.0, 0.0
                m = statistics.mean(xs)
                s = statistics.stdev(xs) if len(xs) > 1 else 0.0
                return m, s
            ar_m, ar_s = mean_std(col("actual_rps"))
            sr_m, _    = mean_std(col("success_rate"))
            p50_m, p50_s = mean_std(col("p50_ms"))
            p95_m, p95_s = mean_std(col("p95_ms"))
            p99_m, p99_s = mean_std(col("p99_ms"))
            lines.append(
                f"{rps:>6}  {policy:<14}  "
                f"{ar_m:>7.0f}±{ar_s:<3.0f}  "
                f"{sr_m*100:>6.2f}%  "
                f"{p50_m:>5.1f}±{p50_s:<3.1f}  "
                f"{p95_m:>5.1f}±{p95_s:<3.1f}  "
                f"{p99_m:>5.1f}±{p99_s:<3.1f}"
            )

report = "\n".join(lines) + "\n"
print(report)
with open(report_file, "w") as f:
    f.write(report)
print(f"wrote {report_file}")
PYEOF

log "All done → ${OUTPUT_DIR}/"
