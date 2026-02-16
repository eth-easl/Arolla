# Custom test logic for the canary routing demo.
# Sourced by deploy-app.sh — has access to: gateway_url, APP_HOST, CYAN, GREEN, NC, etc.

run_app_tests() {
    local gateway_url="$1"

    echo -e "${CYAN}--- Canary traffic split (10 requests) ---${NC}"
    echo ""
    local response
    for i in $(seq 1 10); do
        response=$(curl -s --connect-timeout 5 -H "Host: ${APP_HOST}" "${gateway_url}/" 2>/dev/null) || response="(request failed)"
        echo "  [$i] $response"
    done
    echo ""
    echo -e "${CYAN}Expected: roughly 50/50 split between v1 and v2.${NC}"

    echo ""
    echo -e "${CYAN}--- Single request with verbose headers ---${NC}"
    echo ""
    curl -sv --connect-timeout 5 -H "Host: ${APP_HOST}" "${gateway_url}/" 2>&1 || true
}
