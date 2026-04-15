#!/usr/bin/env bash
# Verify the retry_budget is actually applied to the cartservice cluster,
# and (optionally) that the applied values match what's in a policy YAML.
#
# Usage:
#   verify_retry_budget.sh [frontend|cartservice|...] [--policy-yaml <path>]
#
# Without --policy-yaml: just asserts "retry_budget is applied" (any value).
# With    --policy-yaml: also asserts live values match the YAML. Exits
# non-zero on mismatch so sweeps/grids fail loudly instead of silently
# producing data for the wrong parameter.

set -euo pipefail

SVC="frontend"
POLICY_YAML=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --policy-yaml) POLICY_YAML="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
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
import json, sys
from pathlib import Path

dump_path = sys.argv[1]
policy_yaml = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

try:
    with open(dump_path) as f:
        d = json.load(f)
except (json.JSONDecodeError, IOError) as e:
    print(f"[verify] could not parse config_dump: {e}", file=sys.stderr)
    sys.exit(2)

# Extract expected values from the policy YAML if provided. We only care
# about the two leaf fields the retryBudget DestinationRule supports.
expected = {}
if policy_yaml:
    try:
        import yaml
        with open(policy_yaml) as f:
            doc = yaml.safe_load(f)
        rb = doc.get('spec', {}).get('trafficPolicy', {}).get('retryBudget', {})
        if 'percent' in rb:
            expected['budget_percent'] = float(rb['percent'])
        if 'minRetryConcurrency' in rb:
            expected['min_retry_concurrency'] = int(rb['minRetryConcurrency'])
    except Exception as e:
        print(f"[verify] could not parse {policy_yaml}: {e}", file=sys.stderr)
        sys.exit(2)
    if not expected:
        print(f"[verify] no retryBudget fields found in {policy_yaml}", file=sys.stderr)
        sys.exit(2)

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
    cb = cluster.get('circuit_breakers', {})
    thresholds = cb.get('thresholds', [])
    print(f"  thresholds: {len(thresholds)} entry/entries")
    for i, t in enumerate(thresholds):
        priority = t.get('priority', 'DEFAULT')
        budget = t.get('retry_budget')
        print(f"    [{i}] priority={priority}")
        if budget:
            bp = budget.get('budget_percent', {}).get('value', '?')
            mrc = budget.get('min_retry_concurrency', '?')
            print(f"        retry_budget: budget_percent={bp}, min_retry_concurrency={mrc}")
        else:
            print(f"        retry_budget: (none)")
        for k in ('max_retries','max_requests','max_pending_requests','max_connections'):
            v = t.get(k)
            if v is not None: print(f"        {k}: {v}")

    UNLIMITED = 4294967295
    default_thresholds = [t for t in thresholds if t.get('priority','DEFAULT') == 'DEFAULT']
    with_budget = [t for t in default_thresholds if t.get('retry_budget')]
    without_budget = [t for t in default_thresholds if not t.get('retry_budget')]

    print()
    if with_budget and not without_budget:
        print("  ✓ retry_budget is applied — single DEFAULT threshold with budget.")
        if expected:
            actual_budget = with_budget[0]['retry_budget']
            actual = {
                'budget_percent': actual_budget.get('budget_percent', {}).get('value', 0.0),
                'min_retry_concurrency': actual_budget.get('min_retry_concurrency', 0),
            }
            for k, want in expected.items():
                got = actual.get(k)
                if isinstance(want, float):
                    ok = got is not None and abs(float(got) - want) < 1e-6
                else:
                    ok = got == want
                if ok:
                    print(f"    ✓ {k}: {got} (expected {want})")
                else:
                    print(f"    ✗ {k}: live={got} expected={want}")
                    mismatches.append((cluster.get('name'), k, got, want))
    elif with_budget and without_budget:
        print("  ✗ DUPLICATED DEFAULT thresholds: budget patch is NOT effective (first entry wins).")
        mismatches.append((cluster.get('name'), 'threshold-duplication', None, None))
    else:
        effective = [t for t in default_thresholds
                     if t.get('max_retries') is not None
                     and t.get('max_retries') < UNLIMITED]
        if effective:
            print(f"  ✓ retry cap via max_retries={effective[0]['max_retries']}")
        else:
            print("  ✗ no retry cap (no budget, max_retries unlimited).")
            mismatches.append((cluster.get('name'), 'no-cap', None, None))

if not found:
    print("[verify] no cartservice cluster found in config_dump", file=sys.stderr)
    sys.exit(3)
if mismatches:
    print(f"\n[verify] FAILED: {len(mismatches)} mismatch(es)", file=sys.stderr)
    sys.exit(4)
PYEOF
