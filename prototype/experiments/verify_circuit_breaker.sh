#!/usr/bin/env bash
# Verify the circuit-breaker (outlier detection) EnvoyFilter is actually
# applied to a service's upstream cluster.
#
# Usage: verify_circuit_breaker.sh [frontend|cartservice|...]

set -euo pipefail

SVC="${1:-frontend}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${PROTO_DIR}/k8s-config.sh"

echo "[verify] sampling Envoy config from $SVC sidecar"
POD=$(ssh ${SSH_OPTS} "${SSH_USER}@${MASTER_HOST}" \
  "kubectl -n online-boutique get pod -l app=$SVC -o name | head -1 | sed 's|pod/||'")
echo "[verify] pod: $POD"
if [[ -z "${POD}" ]]; then
  echo "[verify] no pod found for app=$SVC" >&2
  exit 2
fi

TMP_DUMP="$(mktemp)"
trap "rm -f ${TMP_DUMP}" EXIT
ssh ${SSH_OPTS} "${SSH_USER}@${MASTER_HOST}" \
  "kubectl -n online-boutique exec $POD -c istio-proxy -- \
    curl -s 'localhost:15000/config_dump'" > "${TMP_DUMP}"

echo "[verify] config_dump size: $(wc -c < ${TMP_DUMP}) bytes"

python3 - "${TMP_DUMP}" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except (json.JSONDecodeError, IOError) as e:
    print(f"[verify] could not parse config_dump: {e}", file=sys.stderr)
    sys.exit(2)

all_clusters = []
for block in d.get('configs', []):
    if not block.get('@type', '').endswith('ClustersConfigDump'):
        continue
    for c in block.get('dynamic_active_clusters', []):
        all_clusters.append(c.get('cluster', {}))
    for c in block.get('static_clusters', []):
        all_clusters.append(c.get('cluster', {}))

# Fields we expect the circuit-breaker EnvoyFilter to set on outlier_detection.
EXPECTED = {
    "failure_percentage_threshold",
    "enforcing_failure_percentage",
    "failure_percentage_minimum_hosts",
    "failure_percentage_request_volume",
    "enforcing_consecutive_5xx",
    "interval",
    "base_ejection_time",
    "max_ejection_time",
    "max_ejection_percent",
}

found = False
for cluster in all_clusters:
    name = cluster.get('name', '')
    if 'cartservice' not in name or 'outbound' not in name:
        continue
    found = True
    print(f"\n=== {name} ===")
    od = cluster.get('outlier_detection')
    if not od:
        print("  outlier_detection: (none)")
        print()
        print("  ✗ circuit-breaker is NOT applied.")
        continue

    print("  outlier_detection:")
    for k in sorted(od):
        print(f"    {k}: {od[k]}")

    # Assessment: the policy is effective if the failure-rate fields are set
    # to non-default values and enforcing_failure_percentage > 0.
    threshold = od.get("failure_percentage_threshold", 0)
    enforcing = od.get("enforcing_failure_percentage", 0)
    present_fields = set(od.keys())
    print()
    if threshold > 0 and enforcing > 0:
        print(f"  ✓ circuit-breaker is applied — failure_percentage_threshold={threshold}, "
              f"enforcing={enforcing}%.")
        missing = EXPECTED - present_fields
        if missing:
            print(f"    (note: expected fields missing from config: {sorted(missing)})")
    else:
        print(f"  ✗ circuit-breaker is NOT effective "
              f"(threshold={threshold}, enforcing={enforcing}).")

if not found:
    print("[verify] no cartservice cluster found in config_dump", file=sys.stderr)
    sys.exit(3)
PYEOF
