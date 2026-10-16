#!/usr/bin/env bash
# FIX #1: Verify the exact model_name labels emitted by vLLM and SGLang before
# applying KEDA ScaledObjects. A label mismatch causes KEDA to always return 0,
# silently disabling autoscaling — the most critical bug in the reference platform.
set -euo pipefail

echo "==> Verifying vLLM metric labels..."

for model in llama3 qwen3; do
  POD=$(kubectl get pods -n llm-serving -l "app=vllm-${model}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -z "$POD" ]; then
    echo "  [SKIP] vllm-${model}: no running pod found"
    continue
  fi

  echo -n "  vllm-${model} (pod $POD): "
  LABELS=$(kubectl exec -n llm-serving "$POD" -- \
    sh -c 'curl -s http://localhost:8000/metrics' | \
    grep 'vllm:num_requests_waiting' | \
    grep -oP 'model_name="[^"]+"' | head -1 || true)

  if [ -z "$LABELS" ]; then
    echo "WARNING — metric vllm:num_requests_waiting not found yet (pod may still be loading)"
  else
    ACTUAL=$(echo "$LABELS" | grep -oP '"[^"]+"' | tr -d '"')
    echo "model_name=\"${ACTUAL}\""

    EXPECTED="${model}"
    if [ "$ACTUAL" != "$EXPECTED" ]; then
      echo "  MISMATCH: ScaledObject expects '${EXPECTED}' but vLLM emits '${ACTUAL}'"
      echo "  Patching scaled-object.yaml..."
      sed -i "s/model_name=\"${EXPECTED}\"/model_name=\"${ACTUAL}\"/g" \
        "$(dirname "$0")/../k8s/vllm/${model}/scaled-object.yaml"
      echo "  Patched k8s/vllm/${model}/scaled-object.yaml"
    else
      echo "  OK — label matches ScaledObject"
    fi
  fi
done

echo ""
echo "==> Verifying SGLang metric labels..."

for model in llama3 qwen3; do
  POD=$(kubectl get pods -n llm-serving -l "app=sglang-${model}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -z "$POD" ]; then
    echo "  [SKIP] sglang-${model}: no running pod found"
    continue
  fi

  echo -n "  sglang-${model} (pod $POD): "
  LABELS=$(kubectl exec -n llm-serving "$POD" -- \
    sh -c 'curl -s http://localhost:30000/metrics' | \
    grep 'num_queue_reqs' | \
    grep -oP 'model_name="[^"]+"' | head -1 || true)

  if [ -z "$LABELS" ]; then
    echo "WARNING — sglang metric not found yet"
  else
    ACTUAL=$(echo "$LABELS" | grep -oP '"[^"]+"' | tr -d '"')
    EXPECTED="${model}-sglang"
    echo "model_name=\"${ACTUAL}\""
    if [ "$ACTUAL" != "$EXPECTED" ]; then
      echo "  MISMATCH — patching k8s/sglang/${model}/scaled-object.yaml"
      sed -i "s/model_name=\"${EXPECTED}\"/model_name=\"${ACTUAL}\"/g" \
        "$(dirname "$0")/../k8s/sglang/${model}/scaled-object.yaml"
    else
      echo "  OK"
    fi
  fi
done

echo ""
echo "==> Done. Re-apply ScaledObjects if any patches were made:"
echo "    kubectl apply -k k8s/vllm/ k8s/sglang/"
