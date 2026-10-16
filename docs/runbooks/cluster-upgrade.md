# Runbook: Cluster Upgrade

## vLLM version upgrade

FIX #9: There is no `ignore_changes` on deployment manifests. Changes to `deployment.yaml` apply immediately on the next `kubectl apply`.

```bash
# 1. Edit the image tag in k8s/vllm/llama3/deployment.yaml
# 2. Apply and watch rollout
make redeploy-vllm
kubectl rollout status deployment/vllm-llama3 -n llm-serving --timeout=600s
```

## SGLang version upgrade
```bash
# Edit k8s/sglang/*/deployment.yaml image tag
make redeploy-sglang
```

## k3s upgrade (Vast.ai)
```bash
# On the control plane node:
ssh root@<cp-ip>
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.31.0+k3s1 sh -s - server --secrets-encryption

# On each GPU worker:
ssh root@<worker-ip>
curl -sfL https://get.k3s.io | K3S_URL=https://<cp-ip>:6443 K3S_TOKEN=<token> \
  INSTALL_K3S_VERSION=v1.31.0+k3s1 sh -s - agent
```

## RunPod K8s upgrade
Managed by RunPod. Update `version` in `provision/runpod/kubernetes.tf` and apply:
```bash
cd provision/runpod && terraform apply
```
