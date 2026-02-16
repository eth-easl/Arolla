# Custom test logic for the retry budget demo.
# Sourced by deploy-app.sh — has access to: gateway_url, APP_HOST, APP_NS,
#   APP_GATEWAY_NAME, MASTER_HOST, CYAN, GREEN, NC, remote(), remote_capture(), info(), ok(), etc.

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

    # ── Send traffic ─────────────────────────────────────────────────────
    echo -e "${CYAN}--- Sending ${num_requests} requests ---${NC}"
    echo ""
    local ok_count=0 fail_count=0 response status_codes=""
    for i in $(seq 1 "$num_requests"); do
        response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
            -H "Host: ${APP_HOST}" "${gateway_url}/" 2>/dev/null) || response="000"
        status_codes+="$response"$'\n'
        if [[ "$response" == "200" ]]; then
            ((ok_count++)) || true
        else
            ((fail_count++)) || true
        fi
    done

    echo "  Client-side results (what the caller sees):"
    echo "$status_codes" | grep -v '^$' | sort | uniq -c | sort -rn | while read -r count code; do
        echo "    HTTP $code: $count"
    done || true
    echo ""
    echo -e "${GREEN}  ${ok_count} succeeded / ${fail_count} failed out of ${num_requests}${NC}"
    echo ""

    # ── Envoy config check (verify controller generated the right config) ─
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
            echo 'Retry budget (circuit breaker level):'
            kubectl -n ${APP_NS} exec \"\$GATEWAY_POD\" -- \
                curl -s localhost:15000/clusters 2>/dev/null | \
                grep 'backend.${APP_NS}' | \
                grep -E '(max_retries|rq_retry_open)' | \
                sed 's/.*svc.cluster.local::/  /' || echo '  (no circuit breaker stats)'

            echo ''
            echo 'Controller-managed EnvoyFilters:'
            kubectl -n ${APP_NS} get envoyfilter -l app.kubernetes.io/managed-by=btp-controller 2>/dev/null || echo '  (none found — is the controller running?)'
        fi
    "
    echo ""

    # ── Envoy retry stats (what actually happened inside the mesh) ───────
    echo -e "${CYAN}--- Envoy Retry Stats (gateway proxy) ---${NC}"
    echo ""
    remote "$MASTER_HOST" "
        GATEWAY_POD=\$(kubectl -n ${APP_NS} get pod \
            -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true

        if [[ -z \"\$GATEWAY_POD\" ]]; then
            echo '(gateway pod not found)'
        else
            echo 'Per-cluster retry stats:'
            kubectl -n ${APP_NS} exec \"\$GATEWAY_POD\" -- \
                curl -s localhost:15000/stats 2>/dev/null | \
                grep 'backend.${APP_NS}' | \
                grep -E '(upstream_rq_total|upstream_rq_completed|upstream_rq_retry|upstream_rq_200|upstream_rq_503|upstream_rq_5xx)' | \
                sed 's/.*svc.cluster.local./  /' || echo '  (no stats found)'

            echo ''
            echo 'Per-endpoint stats:'
            kubectl -n ${APP_NS} exec \"\$GATEWAY_POD\" -- \
                curl -s localhost:15000/clusters 2>/dev/null | \
                grep 'backend.${APP_NS}' | \
                grep -E '(rq_total|rq_error|rq_success)' | \
                sed 's/.*svc.cluster.local::/  /' || echo '  (no stats found)'
        fi
    "
    echo ""

    # ── Envoy access log tail (per-request detail) ───────────────────────
    echo -e "${CYAN}--- Recent Envoy Access Logs (last 20 lines) ---${NC}"
    echo ""
    echo "  Response flags:  -=ok  URX=retry limit exceeded  UO=retry budget overflow"
    echo ""
    remote "$MASTER_HOST" "
        GATEWAY_POD=\$(kubectl -n ${APP_NS} get pod \
            -l gateway.networking.k8s.io/gateway-name=${APP_GATEWAY_NAME} \
            -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
        if [[ -n \"\$GATEWAY_POD\" ]]; then
            kubectl -n ${APP_NS} logs \"\$GATEWAY_POD\" -c istio-proxy --tail=20 2>/dev/null || echo '(no access logs — enable with: istioctl install --set meshConfig.accessLogFile=/dev/stdout)'
        fi
    "
}
