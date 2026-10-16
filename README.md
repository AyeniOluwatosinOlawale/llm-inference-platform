# llm-inference-platform

Production-grade LLM inference platform targeting commodity GPU clouds — **RunPod** and **Vast.ai**. Serves **Llama 3.1 8B** and **Qwen3 8B** via vLLM behind a LiteLLM proxy gateway, with full observability, autoscaling, and all gaps from the reference EKS platform fixed.

Companion to [inference-optimizer-lab2](https://github.com/AyeniOluwatosinOlawale/inference-optimizer-lab2): the lab measures how inference engines behave; this platform provisions and operates the infrastructure to serve a real production workload.

| Lab (inference-optimizer-lab2) | This platform |
|---|---|
| Measure TTFT and TPOT | Monitor live request latency |
| Sweep concurrency 1→128 | Scale serving replicas as demand changes |
| Measure throughput | Match serving capacity to traffic |
| Study KV-cache reuse | Manage GPU memory and model-serving efficiency |
| Define latency SLOs | Use operational metrics to guide scaling policies |
| Compare GPU performance | Optimise infrastructure cost and availability |

## Architecture

```
Internet
   │
   ▼
[Traefik Ingress + TLS]
   │
   ▼
[LiteLLM Proxy]  ──  Redis (response cache)
   │              ──  PostgreSQL (spend tracking)
   ├─── llama3-service  ──▶  [vLLM: Llama 3.1 8B AWQ]  × KEDA-scaled
   └─── qwen3-service   ──▶  [vLLM: Qwen3 8B fp8]      × KEDA-scaled
                                       │
                              [DCGM GPU Exporter]
                                       │
                              [Prometheus + Grafana + Loki + AlertManager]
```

## Providers

| | RunPod | Vast.ai |
|---|---|---|
| Provisioning | Terraform (`terraform-provider-runpod`) | Python SDK (`vastai`) |
| Kubernetes | RunPod managed K8s | k3s bootstrapped via cloud-init |
| Secrets encryption | KMS CMK | k3s `--secrets-encryption` |
| State backend | S3 + DynamoDB | Supabase Storage |
| GPU (24 GB) | A10G / RTX 4090 | RTX 4090 / A40 |

## Models

| Model | Quantization | VRAM | Endpoint |
|---|---|---|---|
| Llama 3.1 8B Instruct | AWQ 4-bit | ~10 GB | `/v1/chat/completions` via LiteLLM alias `llama3` |
| Qwen3 8B Instruct | fp8 | ~8 GB | `/v1/chat/completions` via LiteLLM alias `qwen3` |

## Quick Start

```bash
# 1. Generate secrets
make secrets

# 2. Provision cluster (choose one)
make up-runpod       # Terraform → RunPod managed K8s
make up-vastai       # Python SDK → Vast.ai instances + k3s

# 3. Deploy platform
make deploy-base     # Namespaces + NetworkPolicies
make deploy-infra    # Prometheus, KEDA, Traefik, Loki (Helm)
make verify-metrics  # Confirm vLLM metric labels before KEDA wires up
make deploy-vllm     # Llama3 + Qwen3 deployments
make deploy-litellm  # LiteLLM gateway
make deploy-dashboards

# 4. Verify
make health-check
make load-test

# 5. Bridge to lab benchmarks
make benchmark-prod            # Run lab scenarios against live platform
make benchmark-compare \
  LAB=path/to/lab_results.json \
  PROD=path/to/prod_results.json
```

## Gap Fixes vs Reference EKS Platform

See [docs/gap-fixes.md](docs/gap-fixes.md) for the full mapping. Summary:

| # | Severity | Fix |
|---|---|---|
| 1 | HIGH | KEDA metric label verified by `scripts/verify-metrics.sh` before deploy |
| 2 | HIGH | etcd Secrets encrypted (k3s `--secrets-encryption` / RunPod KMS) |
| 3 | HIGH | Remote state: S3+DynamoDB (RunPod) / Supabase Storage (Vast.ai) |
| 4 | HIGH | Burst throttling eliminated — per-node NVMe HF cache, no shared NFS |
| 5 | MED | 6 PrometheusRules wired to AlertManager |
| 6 | MED | Egress NetworkPolicies on all namespaces |
| 7 | MED | Redis `requirepass` + AOF persistence |
| 8 | MED | `startupProbe` replaces fragile `initialDelaySeconds`; warmup is best-effort |
| 9 | MED | No `ignore_changes` footgun; explicit `make redeploy-vllm` target |
| 10 | MED | K8s API endpoint private by default on both providers |
| 11 | LOW | 8 Terraform outputs (was 2) |
| 12 | LOW | Prometheus `retentionSize: "45GB"` cap |
| 13 | LOW | 3 Grafana dashboards provisioned as ConfigMaps |
| 14 | LOW | Loki + FluentBit log aggregation stack |

## Directory Structure

```
provision/runpod/        Terraform for RunPod managed K8s
provision/vastai/        Python SDK provisioner + k3s cloud-init
k8s/base/                Namespaces + NetworkPolicies (ingress + egress)
k8s/vllm/llama3/         Llama 3.1 8B deployment, service, KEDA ScaledObject
k8s/vllm/qwen3/          Qwen3 8B deployment, service, KEDA ScaledObject
k8s/litellm/             Gateway, Redis (auth+AOF), Postgres, HPA
k8s/monitoring/          PrometheusRules, Loki, Grafana dashboards
helm/                    Helm values overrides
scripts/                 verify-metrics, generate-secrets, health-check, load-test
benchmark/               Bridge to inference-optimizer-lab2
docs/                    Architecture, cost profile, gap fixes, runbooks
```
