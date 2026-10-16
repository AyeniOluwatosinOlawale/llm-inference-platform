# Architecture

## Component Diagram

```
                         Internet
                             │
                    ┌────────▼────────┐
                    │  Traefik Ingress │  TLS via cert-manager / Let's Encrypt
                    │  (2 replicas)    │
                    └────────┬────────┘
                             │ HTTPS
                    ┌────────▼────────┐
                    │  LiteLLM Proxy  │  Routing · Rate-limiting · Cost tracking
                    │  (2-6 replicas, │  Redis response cache (auth + AOF)
                    │   HPA on CPU)   │  PostgreSQL spend log
                    └────┬───┬───┬────┘
                         │   │   │
          ┌──────────────┘   │   └──────────────┐
          │                  │                  │
   ┌──────▼──────┐    ┌──────▼──────┐    ┌──────▼──────┐
   │  vLLM       │    │  SGLang     │    │  vLLM       │
   │  Llama3.1 8B│    │  Llama3.1 8B│    │  Qwen3 8B   │  ...
   │  v0.30.0    │    │  v0.5.20    │    │  v0.30.0    │
   │  AWQ 4-bit  │    │  AWQ 4-bit  │    │  fp8        │
   │  KEDA-scaled│    │  KEDA-scaled│    │  KEDA-scaled│
   └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
          │                  │                  │
          └─────────┬────────┘                  │
                    │    Per-node NVMe cache     │
                    │    (/var/lib/models)       │
                    │                           │
         ┌──────────▼───────────────────────────▼──────┐
         │         Prometheus (kube-prometheus-stack)   │
         │         + DCGM GPU Exporter                  │
         │         + Grafana (3 dashboards)             │
         │         + AlertManager (6 rules)             │
         │         + Loki + FluentBit (log aggregation) │
         └──────────────────────────────────────────────┘
```

## KEDA Autoscaling

Each deployment (4 total: vLLM×2 + SGLang×2) has a KEDA ScaledObject with two Prometheus triggers:
1. `num_requests_waiting > 5` — queue building up
2. `gpu_cache_usage_perc > 80%` — KV-cache pressure

KEDA polls every 30s. `cooldownPeriod: 300s` prevents thrashing. GPU nodes are on-demand only.

## Model Weight Caching

All four pods share the same `hostPath: /var/lib/models` per node. The init container checks for the model directory before downloading — making repeat starts (pod restarts, rolling updates) fast. First boot: ~5 min download. Subsequent starts: ~30s load.

## Engine Selection via LiteLLM

| Alias | Routes to |
|---|---|
| `llama3-vllm` | vLLM Llama 3.1 8B |
| `llama3-sglang` | SGLang Llama 3.1 8B |
| `qwen3-vllm` | vLLM Qwen3 8B |
| `qwen3-sglang` | SGLang Qwen3 8B |
| `llama3` | Round-robins vLLM + SGLang (A/B traffic) |
| `qwen3` | Round-robins vLLM + SGLang |

## Lab2 Integration

```
inference-optimizer-lab2/
  src/inference_optimizer/
    engines/vllm.py       ──▶  VLLMClient(base_url, model)
    engines/sglang.py     ──▶  SGLangClient(base_url, model)
    sweep/runner.py       ──▶  concurrency sweeps, ctx length sweeps

llm-inference-platform/
  benchmark/
    production_runner.py  ──▶  imports VLLMClient + SGLangClient from lab2
                               points base_url at live cluster services
                               runs same sweep config against production
    compare.py            ──▶  loads lab2 results/*.json + production JSON
                               prints side-by-side TTFT / TPOT / throughput
```

## Provider Comparison

| | RunPod | Vast.ai |
|---|---|---|
| K8s | Managed (RunPod K8s) | k3s on provisioned instances |
| Provisioning | Terraform | Python SDK (`vastai`) |
| Remote state | S3 + DynamoDB | Supabase Storage |
| Secrets encryption | KMS CMK | k3s `--secrets-encryption` |
| Persistent storage | RunPod Network Volumes | hostPath NVMe per node |
| GPU types (24 GB) | A10G, RTX A4000 | RTX 4090, A40, RTX 3090 |
