#!/usr/bin/env bash
# Verify the circuit-breaker (outlier detection) EnvoyFilter is actually
# applied to a service's upstream cluster, and optionally that live values
# match a policy YAML.
#
# Usage:
#   verify_circuit_breaker.sh [frontend|cartservice|...] [--policy-yaml <path>]

set -euo pipefail

SVC="frontend"
POLICY_YAML=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --policy-yaml) POLICY_YAML="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) SVC="$1"; shift ;;
  esac
done

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
[[ -n "${POLICY_YAML}" ]] && echo "[verify] expected values from: ${POLICY_YAML}"

python3 - "${TMP_DUMP}" "${POLICY_YAML}" <<'PYEOF'
import json, sys, re

dump_path = sys.argv[1]
policy_yaml = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

try:
    with open(dump_path) as f:
        d = json.load(f)
except (json.JSONDecodeError, IOError) as e:
    print(f"[verify] could not parse config_dump: {e}", file=sys.stderr)
    sys.exit(2)

# Extract expected outlier_detection fields from the policy YAML.
# The EnvoyFilter path is spec.configPatches[0].patch.value.outlier_detection.
expected = {}
if policy_yaml:
    try:
        import yaml
        with open(policy_yaml) as f:
            doc = yaml.safe_load(f)
        patches = doc.get('spec', {}).get('configPatches', [])
        for p in patches:
            v = p.get('patch', {}).get('value', {})
            od = v.get('outlier_detection')
            if od:
                expected.update(od)
                break
    except Exception as e:
        print(f"[verify] could not parse {policy_yaml}: {e}", file=sys.stderr)
        sys.exit(2)
    if not expected:
        print(f"[verify] no outlier_detection fields found in {policy_yaml}", file=sys.stderr)
        sys.exit(2)

def norm_duration(v):
    """Normalize '5s', '500ms', 5, '5' to seconds (float), or None."""
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip()
    m = re.fullmatch(r'(\d+(?:\.\d+)?)(s|ms|us|m|h)?', s)
    if not m: return None
    num, unit = float(m.group(1)), m.group(2) or 's'
    mult = {'us':1e-6,'ms':1e-3,'s':1,'m':60,'h':3600}[unit]
    return num * mult

DURATION_FIELDS = {'interval','base_ejection_time','max_ejection_time'}

all_clusters = []
for block in d.get('configs', []):
    if not block.get('@type', '').endswith('ClustersConfigDump'):
        continue
    for c in block.get('dynamic_active_clusters', []):
        all_clusters.append(c.get('cluster', {}))
    for c in block.get('static_clusters', []):
        all_clusters.append(c.get('cluster', {}))

found = False
mismatches = []
for cluster in all_clusters:
    name = cluster.get('name', '')
    if 'cartservice' not in name or 'outbound' not in name:
        continue
    found = True
    print(f"\n=== {name} ===")
    od = cluster.get('outlier_detection')
    if not od:
        print("  outlier_detection: (none)")
        print("  ✗ circuit-breaker is NOT applied.")
        mismatches.append((name, 'not-applied', None, None))
        continue

    print("  outlier_detection:")
    for k in sorted(od):
        print(f"    {k}: {od[k]}")
    print()

    threshold = od.get("failure_percentage_threshold", 0)
    enforcing = od.get("enforcing_failure_percentage", 0)
    consec   = od.get("consecutive_5xx", 0)
    enf_cons = od.get("enforcing_consecutive_5xx", 0)

    rate_effective  = threshold > 0 and enforcing > 0
    count_effective = consec > 0 and enf_cons > 0
    if rate_effective:
        print(f"  ✓ circuit-breaker (rate-based) is applied — threshold={threshold}, enforcing={enforcing}%")
    elif count_effective:
        print(f"  ✓ circuit-breaker (count-based) is applied — consecutive_5xx={consec}, enforcing={enf_cons}%")
    else:
        print(f"  ✗ circuit-breaker is NOT effective (no rate or count mode enabled).")
        mismatches.append((name, 'not-effective', None, None))

    if expected:
        print()
        print("  value check (policy YAML vs live):")
        for k, want in expected.items():
            got = od.get(k)
            if k in DURATION_FIELDS:
                nw, ng = norm_duration(want), norm_duration(got)
                ok = nw is not None and ng is not None and abs(nw - ng) < 1e-6
            else:
                ok = str(got) == str(want)
            if ok:
                print(f"    ✓ {k}: {got} (expected {want})")
            else:
                print(f"    ✗ {k}: live={got} expected={want}")
                mismatches.append((name, k, got, want))

if not found:
    print("[verify] no cartservice cluster found in config_dump", file=sys.stderr)
    sys.exit(3)
if mismatches:
    print(f"\n[verify] FAILED: {len(mismatches)} mismatch(es)", file=sys.stderr)
    sys.exit(4)
PYEOF
