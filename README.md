# llm-inference-platform

Production-grade LLM inference platform on **AWS EKS**. Serves **Llama 3.1 8B** and **Qwen3 8B** on both **vLLM v0.30.0** and **SGLang v0.5.20** behind a LiteLLM proxy gateway, with full observability, autoscaling, and cluster backup.

Companion to [inference-optimizer-lab2](https://github.com/AyeniOluwatosinOlawale/inference-optimizer-lab2): the lab measures how inference engines behave at different concurrency levels; this platform provisions and operates the infrastructure to serve a real production workload.

| Lab (inference-optimizer-lab2) | This platform |
|---|---|
| Measure TTFT and TPOT | Monitor live request latency |
| Sweep concurrency 1→128 | Scale serving replicas as demand changes |
| Measure throughput | Match serving capacity to traffic |
| Study KV-cache reuse | Manage GPU memory and model-serving efficiency |
| Define latency SLOs | Use operational metrics to guide scaling policies |
| Compare vLLM vs SGLang | Serve both engines in production, compare live |

## Architecture

```
Internet
   │
   ▼
[Traefik Ingress + TLS]
   │
   ▼
[LiteLLM Proxy]  ──  Redis (response cache, AOF persistence)
   │              ──  PostgreSQL (spend tracking, daily backup)
   ├─── llama3-vllm    ──▶  [vLLM v0.30.0:  Llama 3.1 8B AWQ]  × KEDA-scaled
   ├─── llama3-sglang  ──▶  [SGLang v0.5.20: Llama 3.1 8B AWQ]  × KEDA-scaled
   ├─── qwen3-vllm     ──▶  [vLLM v0.30.0:  Qwen3 8B fp8]       × KEDA-scaled
   └─── qwen3-sglang   ──▶  [SGLang v0.5.20: Qwen3 8B fp8]       × KEDA-scaled
                                      │
                             [DCGM GPU Exporter]
                                      │
                   [Prometheus + Grafana + Loki + AlertManager]
```

Virtual aliases `llama3` and `qwen3` round-robin across both engines for live A/B traffic splitting.

## Providers

| | AWS EKS (primary) | RunPod | Vast.ai |
|---|---|---|---|
| Provisioning | Terraform (hashicorp/aws + eks module) | Terraform (`terraform-provider-runpod`) | Python SDK (`vastai`) |
| Kubernetes | EKS 1.30 managed | RunPod managed K8s | k3s bootstrapped via cloud-init |
| Secrets encryption | KMS CMK (etcd) | KMS CMK | k3s `--secrets-encryption` |
| State backend | S3 + DynamoDB | S3 + DynamoDB | Supabase Storage |
| GPU (24 GB) | A10G (g5.xlarge) | A10G / RTX 4090 | RTX 4090 / A40 |
| Cost/hr (2 GPU nodes) | ~$2.10 | ~$1.60 | ~$1.20 |

## Models & Engines

| Model | Engine | Quantization | VRAM | LiteLLM Alias |
|---|---|---|---|---|
| Llama 3.1 8B Instruct | vLLM v0.30.0 | AWQ 4-bit | ~10 GB | `llama3-vllm` |
| Llama 3.1 8B Instruct | SGLang v0.5.20 | AWQ 4-bit | ~10 GB | `llama3-sglang` |
| Qwen3 8B Instruct | vLLM v0.30.0 | fp8 | ~8 GB | `qwen3-vllm` |
| Qwen3 8B Instruct | SGLang v0.5.20 | fp8 | ~8 GB | `qwen3-sglang` |

## Platform Features

- **KEDA autoscaling** on Prometheus queue depth and KV-cache pressure — `scripts/verify-metrics.sh` confirms metric label alignment before any ScaledObject is applied
- **Secrets encrypted at rest** — k3s `--secrets-encryption` (Vast.ai) and KMS CMK (RunPod)
- **Remote state** — S3 + DynamoDB (RunPod Terraform) and Supabase Storage (Vast.ai)
- **Per-node NVMe model cache** — init container checks `/var/lib/models` before downloading; repeat starts take ~30s instead of ~5 min
- **6 PrometheusRules** covering queue spikes, KV-cache saturation, pod crash loops, pod not-ready, and Prometheus disk pressure
- **Egress NetworkPolicies** on all namespaces — explicit allowlist for DNS, HuggingFace HTTPS, and inter-service traffic
- **Redis** with `requirepass` auth and AOF persistence on a PVC
- **PostgreSQL** daily `pg_dump` to a 20Gi PVC with 7-day retention
- **Velero** daily cluster-state and PVC backup to S3 — covers `llm-serving`, `llm-gateway`, and `monitoring` namespaces
- **3 Grafana dashboards** provisioned as ConfigMaps: engine comparison, GPU utilization, LiteLLM gateway
- **Loki + FluentBit** log aggregation across all serving and gateway pods
- **AlertManager** Slack notifications — warnings to `#llm-platform-alerts`, critical to `#llm-platform-critical`
- **LiteLLM HPA** scales the proxy tier 2→6 replicas at 70% CPU

## Quick Start

```bash
# 1. Generate secrets
make secrets
# Edit .env.secrets: set SLACK_WEBHOOK_URL, VELERO_BACKUP_BUCKET, HUGGINGFACE_TOKEN

# 2. Provision cluster (choose one)
make up-eks          # Terraform → AWS EKS (g5.xlarge GPU nodes, recommended)
make up-runpod       # Terraform → RunPod managed K8s
make up-vastai       # Python SDK → Vast.ai instances + k3s

# 3. Deploy platform
make deploy-base      # Namespaces + NetworkPolicies
make deploy-infra     # Prometheus, KEDA, Traefik, Loki (Helm)
make deploy-velero    # Velero cluster + PVC backup
make verify-metrics   # Confirm vLLM/SGLang metric labels before KEDA wires up
make deploy-vllm      # Llama3 + Qwen3 on vLLM
make deploy-sglang    # Llama3 + Qwen3 on SGLang
make deploy-litellm   # LiteLLM gateway
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

## Directory Structure

```
provision/eks/           Terraform for AWS EKS (VPC, KMS, GPU + CPU node groups)
provision/runpod/        Terraform for RunPod managed K8s
provision/lambda/        Lambda Labs kubeadm provisioner + cloud-init
provision/vastai/        Python SDK provisioner + k3s cloud-init
k8s/base/                Namespaces + NetworkPolicies (ingress + egress)
k8s/vllm/llama3/         Llama 3.1 8B on vLLM — deployment, service, KEDA ScaledObject
k8s/vllm/qwen3/          Qwen3 8B on vLLM — deployment, service, KEDA ScaledObject
k8s/sglang/llama3/       Llama 3.1 8B on SGLang — deployment, service, KEDA ScaledObject
k8s/sglang/qwen3/        Qwen3 8B on SGLang — deployment, service, KEDA ScaledObject
k8s/litellm/             Gateway, Redis (auth+AOF+PVC), Postgres (PVC backup), HPA
k8s/monitoring/          PrometheusRules, Loki, Grafana dashboards
helm/                    Helm values: kube-prometheus-stack, keda, traefik, loki, velero
scripts/                 verify-metrics, generate-secrets, health-check, load-test
benchmark/               Bridge to inference-optimizer-lab2
docs/                    Architecture, cost profile, runbooks
```
