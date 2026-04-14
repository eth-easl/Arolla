#!/usr/bin/env bash
# Verify the retry_budget EnvoyFilter is actually applied to the cartservice cluster.
# Usage: verify_retry_budget.sh [frontend|cartservice|...]

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

# Find ClustersConfigDump and collect clusters from dynamic + static lists.
all_clusters = []
for block in d.get('configs', []):
    if not block.get('@type', '').endswith('ClustersConfigDump'):
        continue
    for c in block.get('dynamic_active_clusters', []):
        all_clusters.append(c.get('cluster', {}))
    for c in block.get('static_clusters', []):
        all_clusters.append(c.get('cluster', {}))

found = False
for cluster in all_clusters:
    name = cluster.get('name', '')
    if 'cartservice' not in name:
        continue
    if 'outbound' not in name:
        continue
    found = True
    print(f"\n=== {name} ===")
    cb = cluster.get('circuit_breakers', {})
    thresholds = cb.get('thresholds', [])
    print(f"  thresholds: {len(thresholds)} entry/entries")
    for i, t in enumerate(thresholds):
        priority = t.get('priority', 'DEFAULT')
        max_retries = t.get('max_retries')
        max_requests = t.get('max_requests')
        max_pending = t.get('max_pending_requests')
        max_cx = t.get('max_connections')
        budget = t.get('retry_budget')
        print(f"    [{i}] priority={priority}")
        if budget:
            print(f"        retry_budget: budget_percent={budget.get('budget_percent',{}).get('value','?')}, "
                  f"min_retry_concurrency={budget.get('min_retry_concurrency','?')}")
        else:
            print(f"        retry_budget: (none)")
        if max_retries is not None: print(f"        max_retries: {max_retries}")
        if max_requests is not None: print(f"        max_requests: {max_requests}")
        if max_pending is not None: print(f"        max_pending_requests: {max_pending}")
        if max_cx is not None: print(f"        max_connections: {max_cx}")

    # Summary assessment — retry throttling is effective if EITHER
    # retry_budget is set, OR max_retries is a finite value (< default).
    UNLIMITED = 4294967295
    default_thresholds = [t for t in thresholds
                          if t.get('priority', 'DEFAULT') == 'DEFAULT']
    with_budget = [t for t in default_thresholds if t.get('retry_budget')]
    without_budget = [t for t in default_thresholds if not t.get('retry_budget')]
    effective = [t for t in default_thresholds
                 if t.get('retry_budget') or
                    (t.get('max_retries') is not None and t.get('max_retries') < UNLIMITED)]

    print()
    if with_budget and not without_budget:
        print("  ✓ retry_budget is applied — single DEFAULT threshold with budget.")
    elif with_budget and without_budget:
        print("  ✗ DUPLICATED DEFAULT thresholds: budget patch is NOT effective (first entry wins).")
        print("    Envoy uses thresholds[0] which has no budget.")
    elif effective:
        cap = effective[0].get('max_retries', '?')
        print(f"  ✓ retry cap applied via max_retries={cap} (DestinationRule-style).")
    elif without_budget and not with_budget:
        print("  ✗ no retry cap (no budget, max_retries is unlimited).")
    else:
        print("  ? no DEFAULT threshold found — unexpected.")

if not found:
    print("[verify] no cartservice cluster found in config_dump", file=sys.stderr)
    sys.exit(3)
PYEOF
