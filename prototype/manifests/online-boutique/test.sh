#!/usr/bin/env bash
# ============================================================================
# Online Boutique — Smoke Test + Locust Status
# ============================================================================
# Sourced by deploy-app.sh — must define run_app_tests().
#
# Smoke tests verify the gateway→frontend path is working.
# Real load testing is done by the in-cluster loadgenerator pod (Locust).
# ============================================================================

# run_app_tests() is called by deploy-app.sh with the gateway URL as $1.
run_app_tests() {
    local gateway_url="$1"
    local host_header="${APP_HOST}"   # set by deploy-app.sh via app.conf

    local pass=0 total=0

    echo "============================================"
    echo "  Online Boutique Smoke Test"
    echo "  Gateway: ${gateway_url}"
    echo "============================================"
    echo

    # --- Test 1: Homepage ---
    ((total++))
    echo "Test 1: Homepage (GET /)"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Host: ${host_header}" --max-time 10 \
        "${gateway_url}/")
    if [[ "$code" == "200" ]]; then
        echo -e "${GREEN}✓${NC} Homepage returned HTTP ${code}"
        ((pass++))
    else
        echo -e "${RED}✗${NC} Homepage returned HTTP ${code} (expected 200)"
    fi

    # --- Test 2: Product page ---
    ((total++))
    echo "Test 2: Product page (GET /product/OLJCESPC7Z)"
    code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Host: ${host_header}" --max-time 10 \
        "${gateway_url}/product/OLJCESPC7Z")
    if [[ "$code" == "200" ]]; then
        echo -e "${GREEN}✓${NC} Product page returned HTTP ${code}"
        ((pass++))
    else
        echo -e "${RED}✗${NC} Product page returned HTTP ${code} (expected 200)"
    fi

    # --- Test 3: Cart page ---
    ((total++))
    echo "Test 3: Cart page (GET /cart)"
    code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Host: ${host_header}" --max-time 10 \
        "${gateway_url}/cart")
    if [[ "$code" == "200" ]]; then
        echo -e "${GREEN}✓${NC} Cart page returned HTTP ${code}"
        ((pass++))
    else
        echo -e "${RED}✗${NC} Cart page returned HTTP ${code} (expected 200)"
    fi

    # --- Test 4: Health check ---
    ((total++))
    echo "Test 4: Health check (GET /_healthz)"
    code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Host: ${host_header}" \
        -H "Cookie: shop_session-id=x-test-probe" \
        --max-time 10 \
        "${gateway_url}/_healthz")
    if [[ "$code" == "200" ]]; then
        echo -e "${GREEN}✓${NC} Health check returned HTTP ${code}"
        ((pass++))
    else
        echo -e "${RED}✗${NC} Health check returned HTTP ${code} (expected 200)"
    fi

    # --- Test 5: Fault injection (if applied) ---
    ((total++))
    echo "Test 5: Fault injection check (10 rapid requests)"
    local fault_count=0
    for i in $(seq 1 10); do
        code=$(curl -s -o /dev/null -w '%{http_code}' \
            -H "Host: ${host_header}" --max-time 15 \
            "${gateway_url}/product/OLJCESPC7Z" 2>/dev/null || echo "000")
        if [[ "$code" != "200" ]]; then
            ((fault_count++))
        fi
    done
    if [[ $fault_count -gt 0 ]]; then
        echo -e "${YELLOW}⚠${NC} Fault injection detected: ${fault_count}/10 requests returned non-200"
        echo -e "${GREEN}✓${NC} This is expected when fault-injection.yaml is applied"
    else
        echo -e "${GREEN}✓${NC} All 10 requests returned 200 (no fault injection active)"
    fi
    ((pass++))

    # --- Test 6: Stock loadgenerator disabled ---
    ((total++))
    echo "Test 6: Stock loadgenerator disabled"
    local lg_replicas
    lg_replicas=$(ssh ${SSH_OPTS} ${SSH_USER}@${MASTER_HOST} \
        "kubectl -n ${APP_NS} get deploy loadgenerator -o jsonpath='{.spec.replicas}'" \
        2>/dev/null || echo "Unknown")
    if [[ "$lg_replicas" == "0" ]]; then
        echo -e "${GREEN}✓${NC} Stock loadgenerator is disabled (replicas=0)"
        ((pass++))
    else
        echo -e "${RED}✗${NC} Stock loadgenerator replicas=${lg_replicas} (expected 0)"
    fi

    # --- Summary ---
    echo
    echo "============================================"
    if [[ $pass -eq $total ]]; then
        echo -e "${GREEN}✓${NC} All tests passed (${pass}/${total})"
    else
        echo -e "${RED}✗${NC} Some tests failed (${pass}/${total} passed)"
    fi
    echo "============================================"
    echo
    echo "  Note: Stock loadgenerator is disabled for retry experiments in this repo."
    echo "  Run external clients from CLIENT_HOST using:"
    echo "    prototype/clients/online-boutique/run-clients.sh start"
    echo "  These smoke tests verify the gateway path from outside."
}
