#!/usr/bin/env bash
# Progressively shift traffic between vLLM and SGLang via LiteLLM weights.
#
# Usage:
#   bash scripts/canary.sh canary  --engine vllm   --weight 90   # 90% vLLM, 10% SGLang
#   bash scripts/canary.sh promote --engine sglang               # 100% SGLang, 0% vLLM
#   bash scripts/canary.sh rollback                              # reset to 50/50
#
# Under the hood: patches the litellm-config ConfigMap then rolls the LiteLLM
# deployment so the new weights are picked up without a full redeploy.
set -euo pipefail

NAMESPACE="llm-gateway"
CONFIGMAP="litellm-config"
DEPLOYMENT="litellm-proxy"

CMD="${1:-}"
ENGINE="${ENGINE:-vllm}"
WEIGHT="${WEIGHT:-90}"

usage() {
  echo "Usage: $0 <canary|promote|rollback> [--engine vllm|sglang] [--weight 0-100]"
  exit 1
}

# Parse flags
shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --engine) ENGINE="$2"; shift 2 ;;
    --weight) WEIGHT="$2"; shift 2 ;;
    *) usage ;;
  esac
done

# Compute weights
case "$CMD" in
  canary)
    if [[ "$ENGINE" == "vllm" ]]; then
      VLLM_W="$WEIGHT"
      SGLANG_W=$((100 - WEIGHT))
    else
      SGLANG_W="$WEIGHT"
      VLLM_W=$((100 - WEIGHT))
    fi
    ;;
  promote)
    if [[ "$ENGINE" == "vllm" ]]; then
      VLLM_W=100; SGLANG_W=0
    else
      SGLANG_W=100; VLLM_W=0
    fi
    ;;
  rollback)
    VLLM_W=1; SGLANG_W=1
    ;;
  *) usage ;;
esac

echo "==> Traffic split: vLLM=${VLLM_W}  SGLang=${SGLANG_W}"

# Fetch current config, patch weights, reapply
CURRENT=$(kubectl get configmap "$CONFIGMAP" -n "$NAMESPACE" -o jsonpath='{.data.config\.yaml}')

# Use python for safe YAML in-place weight patching
PATCHED=$(python3 - <<PYEOF
import sys, yaml, re

raw = """${CURRENT}"""
config = yaml.safe_load(raw)

vllm_w   = int("${VLLM_W}")   if "${VLLM_W}"   != "0" else 0
sglang_w = int("${SGLANG_W}") if "${SGLANG_W}" != "0" else 0

for m in config.get("model_list", []):
    base = m.get("litellm_params", {}).get("api_base", "")
    if "vllm-service" in base:
        m["litellm_params"]["weight"] = vllm_w
    elif "sglang-service" in base:
        m["litellm_params"]["weight"] = sglang_w

print(yaml.dump(config, default_flow_style=False, sort_keys=False))
PYEOF
)

# Write patched config back to the ConfigMap
kubectl create configmap "$CONFIGMAP" \
  -n "$NAMESPACE" \
  --from-literal="config.yaml=${PATCHED}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Rolling restart — LiteLLM reloads config on startup
echo "==> Rolling restart LiteLLM to pick up new weights..."
kubectl rollout restart deployment/"$DEPLOYMENT" -n "$NAMESPACE"
kubectl rollout status  deployment/"$DEPLOYMENT" -n "$NAMESPACE" --timeout=60s

echo "==> Done. Current split: vLLM=${VLLM_W}%  SGLang=${SGLANG_W}%"
