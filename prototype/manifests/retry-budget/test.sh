# Custom test logic for the retry budget demo.
# Sourced by deploy-app.sh — has access to: gateway_url, APP_HOST, APP_NS,
#   APP_GATEWAY_NAME, MASTER_HOST, CLIENT_HOST, CYAN, GREEN, NC,
#   remote(), remote_capture(), info(), ok(), etc.
#
# Client traffic originates from CLIENT_HOST (external load generator).
# Envoy stats/config queries go to MASTER_HOST (K8s control plane).

run_app_tests() {
    local gateway_url="$1"
    local num_requests="${2:-50}"

    # ── Show current config ──────────────────────────────────────────────
    local failure_rate
    failure_rate=$(remote_capture "$MASTER_HOST" "
        kubectl -n ${APP_NS} get deployment backend \
            -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"FAILURE_RATE\")].value}' 2>/dev/null || echo '?'
    " 2>/dev/null || echo "?")
    failure_rate=$(echo "$failure_rate" | tr -d '[:space:]')

    info "Backend FAILURE_RATE: ${failure_rate}%"
    info "HTTPRoute: retry 503, max 3 attempts, 100ms backoff"
    info "RetryBudget: 20% of traffic over 10s window, min 10 retries/s"
    echo ""

    # ── Reset Envoy stats before test ────────────────────────────────────
    remote_capture "$MASTER_HOST" "
        GATEWAY_POD=\$(kubectl -n ${APP_NS} get pod \
            -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
        kubectl -n ${APP_NS} exec \"\$GATEWAY_POD\" -- \
            curl -s -X POST localhost:15000/reset_counters 2>/dev/null || true
    " 2>/dev/null || true
    info "Envoy stats reset."
    echo ""

    # ── Send traffic from client machine ────────────────────────────────
    info "Client machine: ${CLIENT_HOST}"
    echo -e "${CYAN}--- Sending ${num_requests} requests from ${CLIENT_HOST} ---${NC}"
    echo ""
    local status_codes
    status_codes=$(remote_capture "$CLIENT_HOST" "
        for i in \$(seq 1 ${num_requests}); do
            curl -s -o /dev/null -w '%{http_code}\n' --connect-timeout 5 \
                -H 'Host: ${APP_HOST}' '${gateway_url}/' 2>/dev/null || echo '000'
        done
    " 2>/dev/null)

    local ok_count=0 fail_count=0
    while IFS= read -r code; do
        [[ -z "$code" ]] && continue
        if [[ "$code" == "200" ]]; then
            ((ok_count++)) || true
        else
            ((fail_count++)) || true
        fi
    done <<< "$status_codes"

    echo "  Client-side results (what the caller sees):"
    echo "$status_codes" | grep -v '^$' | sort | uniq -c | sort -rn | while read -r count code; do
        echo "    HTTP $code: $count"
    done || true
    echo ""
    echo -e "${GREEN}  ${ok_count} succeeded / ${fail_count} failed out of ${num_requests}${NC}"
    echo ""

    # ── Envoy config verification ────────────────────────────────────────
    echo -e "${CYAN}--- Envoy Config (retry policy + circuit breaker) ---${NC}"
    echo ""
    remote "$MASTER_HOST" "
        GATEWAY_POD=\$(kubectl -n ${APP_NS} get pod \
            -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true

        if [[ -z \"\$GATEWAY_POD\" ]]; then
            echo '(gateway pod not found)'
        else
            echo 'Retry policy (route level):'
            istioctl proxy-config route \"\$GATEWAY_POD\" -n ${APP_NS} -o json 2>/dev/null | \
                python3 -c \"
import json, sys
data = json.load(sys.stdin)
for rc in data:
    for vh in rc.get('virtualHosts', []):
        for route in vh.get('routes', []):
            rp = route.get('route', {}).get('retryPolicy')
            if rp:
                print(f'  retryOn: {rp.get(\\\"retryOn\\\", \\\"(none)\\\")}')
                print(f'  numRetries: {rp.get(\\\"numRetries\\\", \\\"(default)\\\")}')
                codes = rp.get('retriableStatusCodes', [])
                print(f'  retriableStatusCodes: {codes if codes else \\\"(none — retries wont trigger!)\\\"}')
\" 2>/dev/null || echo '  (could not parse)'

            echo ''
            echo 'Retry budget (cluster level):'
            istioctl proxy-config cluster \"\$GATEWAY_POD\" -n ${APP_NS} \
                --fqdn backend.${APP_NS}.svc.cluster.local -o json 2>/dev/null | \
                python3 -c \"
import json, sys
data = json.load(sys.stdin)
for c in data:
    cb = c.get('circuitBreakers', {})
    for t in cb.get('thresholds', []):
        rb = t.get('retryBudget')
        if rb:
            pct = rb.get('budgetPercent', {}).get('value', '?')
            minc = rb.get('minRetryConcurrency', '?')
            print(f'  budgetPercent: {pct}%')
            print(f'  minRetryConcurrency: {minc}')
        else:
            mr = t.get('maxRetries')
            if mr:
                print(f'  maxRetries: {mr} (no retry budget configured!)')
\" 2>/dev/null || echo '  (could not parse)'

            echo ''
            echo 'XBackendTrafficPolicy status:'
            kubectl -n ${APP_NS} get xbackendtrafficpolicies.gateway.networking.x-k8s.io \
                -o jsonpath='{range .items[*]}{.metadata.name}: {.status.ancestors[0].conditions[0].reason}{\"\\n\"}{end}' 2>/dev/null || echo '  (not found)'
        fi
    "
    echo ""

    # ── Envoy retry stats ────────────────────────────────────────────────
    echo -e "${CYAN}--- Retry Stats (gateway proxy → backend) ---${NC}"
    echo ""
    remote "$MASTER_HOST" "
        GATEWAY_POD=\$(kubectl -n ${APP_NS} get pod \
            -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true

        if [[ -z \"\$GATEWAY_POD\" ]]; then
            echo '(gateway pod not found)'
        else
            # Istio 1.27 stat format uses semicolons for tag extraction:
            #   cluster.<cluster_name>;.<stat_name>: <value>
            # Service port (80), not container port (5678)
            CLUSTER='outbound|80||backend.${APP_NS}.svc.cluster.local'
            STAT_PREFIX=\"cluster.\${CLUSTER};.\"

            STATS=\$(kubectl -n ${APP_NS} exec \"\$GATEWAY_POD\" -- \
                curl -s localhost:15000/stats 2>/dev/null)

            # Extract key counters using grep -F (fixed-string; cluster name has | and . chars)
            RQ_TOTAL=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_total:\" | awk '{print \$2}')
            RQ_COMPLETED=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_completed:\" | awk '{print \$2}')
            RQ_RETRY=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_retry:\" | awk '{print \$2}')
            RQ_RETRY_SUCCESS=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_retry_success:\" | awk '{print \$2}')
            RQ_RETRY_OVERFLOW=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_retry_overflow:\" | awk '{print \$2}')
            RQ_RETRY_LIMIT=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_retry_limit_exceeded:\" | awk '{print \$2}')
            RQ_200=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_200:\" | awk '{print \$2}')
            RQ_503=\$(echo \"\$STATS\" | grep -F \"\${STAT_PREFIX}upstream_rq_503:\" | awk '{print \$2}')

            echo 'Cluster-level Envoy stats:'
            echo \"  upstream_rq_total:                \${RQ_TOTAL:-0}   (total upstream requests incl. retries)\"
            echo \"  upstream_rq_completed:            \${RQ_COMPLETED:-0}   (requests completed to client)\"
            echo \"  upstream_rq_200:                  \${RQ_200:-0}\"
            echo \"  upstream_rq_503:                  \${RQ_503:-0}   (503s returned to client after all retries)\"
            echo ''
            echo \"  upstream_rq_retry:                \${RQ_RETRY:-0}   (retry attempts)\"
            echo \"  upstream_rq_retry_success:        \${RQ_RETRY_SUCCESS:-0}   (retries that got 200)\"
            echo \"  upstream_rq_retry_limit_exceeded: \${RQ_RETRY_LIMIT:-0}   (blocked by max retries)\"
            echo \"  upstream_rq_retry_overflow:       \${RQ_RETRY_OVERFLOW:-0}   (blocked by retry budget)\"

            echo ''
            echo 'Per-endpoint stats (/clusters):'
            kubectl -n ${APP_NS} exec \"\$GATEWAY_POD\" -- \
                curl -s localhost:15000/clusters 2>/dev/null | \
                grep 'backend.${APP_NS}' | \
                grep -E '(rq_total|rq_error|rq_success)' | \
                sed 's/.*svc.cluster.local::/  /'

            echo ''
            echo 'Summary:'
            echo \"  Client requests sent:      ${num_requests}\"
            echo \"  Total upstream to backend:  \${RQ_TOTAL:-?}\"
            echo \"  Retries attempted:          \${RQ_RETRY:-?}\"
            echo \"  Retries succeeded:          \${RQ_RETRY_SUCCESS:-?}\"
            echo \"  Retries blocked (budget):   \${RQ_RETRY_OVERFLOW:-0}\"
            echo \"  Retries blocked (max):      \${RQ_RETRY_LIMIT:-0}\"
        fi
    "
    echo ""

    # ── Envoy access log tail ────────────────────────────────────────────
    echo -e "${CYAN}--- Recent Envoy Access Logs (last 20 lines) ---${NC}"
    echo ""
    echo "  Response flags:  -=ok  URX=retry limit exceeded  UO=retry budget overflow"
    echo ""
    remote "$MASTER_HOST" "
        GATEWAY_POD=\$(kubectl -n ${APP_NS} get pod \
            -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
        if [[ -n \"\$GATEWAY_POD\" ]]; then
            kubectl -n ${APP_NS} logs \"\$GATEWAY_POD\" -c istio-proxy --tail=20 2>/dev/null || echo '(no access logs)'
        fi
    "
}
