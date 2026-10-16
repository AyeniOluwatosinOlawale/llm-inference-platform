#!/usr/bin/env bash
# End-to-end platform health check.
# Verifies: pods ready → LiteLLM reachable → both engines respond → metrics scraped.
set -euo pipefail

LITELLM_URL="${LITELLM_URL:-http://localhost:4000}"
LITELLM_KEY="${LITELLM_MASTER_KEY:-}"
PASS=0
FAIL=0

ok()   { echo "  ✓ $1"; ((PASS+=1)); }
fail() { echo "  ✗ $1"; ((FAIL+=1)); }

echo "==> Checking pod readiness..."
for deploy in vllm-llama3 vllm-qwen3 sglang-llama3 sglang-qwen3; do
  READY=$(kubectl get deployment "$deploy" -n llm-serving \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  [ "${READY:-0}" -gt 0 ] && ok "$deploy: $READY ready" || fail "$deploy: not ready"
done

GREADY=$(kubectl get deployment litellm-proxy -n llm-gateway \
  -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
[ "${GREADY:-0}" -gt 0 ] && ok "litellm-proxy: $GREADY ready" || fail "litellm-proxy: not ready"

echo ""
echo "==> Checking LiteLLM gateway..."
if curl -sf "${LITELLM_URL}/health/readiness" > /dev/null; then
  ok "LiteLLM readiness endpoint"
else
  fail "LiteLLM readiness endpoint unreachable"
fi

echo ""
echo "==> Checking model endpoints via LiteLLM..."
for model in llama3-vllm llama3-sglang qwen3-vllm qwen3-sglang; do
  RESP=$(curl -sf "${LITELLM_URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${LITELLM_KEY}" \
    -d "{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}" \
    2>&1 || echo "ERROR")
  echo "$RESP" | grep -q '"choices"' && ok "$model responded" || fail "$model: $RESP"
done

echo ""
echo "==> Checking Prometheus scrape..."
PROM_URL="http://localhost:9090"
SCRAPED=$(curl -sf "${PROM_URL}/api/v1/query?query=up{job=~\".*vllm.*|.*sglang.*\"}" 2>/dev/null | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']['result']))" 2>/dev/null || echo 0)
[ "$SCRAPED" -gt 0 ] && ok "Prometheus scraping $SCRAPED engine targets" || fail "No engine targets scraped by Prometheus"

echo ""
echo "==> Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ] && echo "Platform healthy." || { echo "Platform has issues — check above."; exit 1; }
