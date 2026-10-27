# Production LLM Inference Platform on GKE

![GKE](https://img.shields.io/badge/GKE-1.35-4285F4?logo=google-cloud&logoColor=white)
![vLLM](https://img.shields.io/badge/vLLM-0.32.0-76B900?logo=nvidia&logoColor=white)
![SGLang](https://img.shields.io/badge/SGLang-0.5.20-76B900?logo=nvidia&logoColor=white)
![LiteLLM](https://img.shields.io/badge/LiteLLM-1.40.0-0080FF?logoColor=white)
![KEDA](https://img.shields.io/badge/KEDA-2.x-326CE5?logo=kubernetes&logoColor=white)
![GPU](https://img.shields.io/badge/GPU-NVIDIA%20L4%2024GB-76B900?logo=nvidia&logoColor=white)
![Status](https://img.shields.io/badge/Status-Live-brightgreen)

A production LLM inference platform running on **Google Kubernetes Engine** with a single **NVIDIA L4 GPU**. Serves open-source large language models through **vLLM v0.32.0** and **SGLang v0.5.20** behind a **LiteLLM 1.40.0** proxy gateway with full observability, event-driven autoscaling, and a custom AI agent interface.

This is the infrastructure layer companion to [inference-optimizer-lab2](https://github.com/AyeniOluwatosinOlawale/inference-optimizer-lab2) — where the lab measures engine behavior in isolation, this platform runs the same engines under real user traffic on real GKE infrastructure.

> **Platform is live.** AnaModel chat UI, Grafana dashboards, LiteLLM gateway, and Prometheus metrics are all publicly accessible. 721 real requests processed, 1.25M tokens served.

---

## Architecture

```
User / API Client
        │
        ▼
[Traefik Ingress — TLS termination]    136.65.123.94.nip.io
        │
        ├──▶ api.136.65.123.94.nip.io/agent   ──▶  [AnaModel Agent Server]
        │                                              FastAPI + SSE streaming
        │                                              Tool use: web search, calculator,
        │                                              weather, crypto, GitHub, Stripe
        │
        └──▶ api.136.65.123.94.nip.io/v1      ──▶  [LiteLLM Proxy v1.40.0]
                                                      ├── Redis  (response cache, AOF)
                                                      ├── PostgreSQL (spend tracking)
                                                      └── NexoraHandler (identity scrub)
                                                               │
                                          ┌────────────────────┼────────────────────┐
                                          ▼                    ▼                    ▼
                                  [vLLM 0.32.0]         [SGLang 0.5.20]    [KEDA ScaledObjects]
                                   Qwen3 8B AWQ          (0 replicas,        queue-depth triggers
                                   1× NVIDIA L4          scales on demand)   per model per engine
                                   19.3 GB VRAM used

[Prometheus]  ◀── ServiceMonitors ──  vLLM / LiteLLM / DCGM / Node Exporter / Kube State
     │
     ├──▶ [Grafana]         — 3 custom dashboards + 15 built-in k8s dashboards
     ├──▶ [AlertManager]    — 6 PrometheusRules
     └──▶ [Loki + FluentBit] — log aggregation from llm-serving + llm-gateway
```

---

## What We Built and Achieved

### Platform Delivered

| Component | Status | Detail |
|---|---|---|
| GKE cluster (1.35) | Live | us-central1, 3 nodes (2 CPU + 1 GPU) |
| vLLM 0.32.0 | Live | Qwen3 8B AWQ on NVIDIA L4, always-on |
| SGLang 0.5.20 | Deployed | 0 replicas — scales on demand via KEDA |
| LiteLLM 1.40.0 gateway | Live | Routing, rate limiting, spend tracking, Redis cache |
| AnaModel Agent Server | Live | FastAPI + SSE streaming, 9 built-in tools, 5 integrations |
| AnaChat UI | Live | Browser chat interface at `136.65.123.94.nip.io/agent/chat` |
| Traefik ingress + TLS | Live | HTTPS on all endpoints via nip.io |
| Redis cache | Live | AOF persistence, password auth from K8s Secret |
| PostgreSQL spend log | Live | Daily pg_dump CronJob, 7-day retention |
| Prometheus | Live | 15-day retention, 45GB cap, 27 active scrape targets |
| Grafana | Live | Anonymous access, 3 custom + 15 k8s dashboards |
| AlertManager | Live | 6 PrometheusRules configured |
| Loki + FluentBit | Live | Log aggregation across all namespaces |
| DCGM GPU Exporter | Live | SM utilization, VRAM, power, temperature |
| KEDA autoscaling | Live | Queue-depth triggers, min=0 for non-primary models |
| Identity scrubbing | Live | NexoraHandler strips Qwen/Alibaba from all responses |

### Real Traffic Numbers (Oct 22 – Oct 27)

| Metric | Value |
|---|---|
| Total requests served | 721 |
| Total tokens processed | 1,252,009 |
| Prompt tokens | 1,117,934 |
| Completion tokens | 134,075 |
| Time to first token (live, idle) | ~2s |
| GPU memory in use | 19.3 GB / 22.5 GB |
| GPU temperature | ~49°C |
| GPU power draw | ~33W |

---

## Technology Stack

| Layer | Technology | Version | Function |
|---|---|---|---|
| Cloud | Google Cloud Platform | — | Project: `testing-494308` |
| Kubernetes | GKE | 1.35.6 | Managed cluster, us-central1 |
| GPU | NVIDIA L4 | 24 GB VRAM | On g2-standard-8 node |
| Inference engine 1 | vLLM | 0.32.0 | Continuous batching, PagedAttention |
| Inference engine 2 | SGLang | 0.5.20 | RadixAttention, prefix caching |
| Models | Qwen3 8B AWQ · Llama 3.1 8B AWQ | — | Quantized to fit L4 VRAM |
| API Gateway | LiteLLM Proxy | 1.40.0 | Routing, cost tracking, Redis cache |
| Agent Server | FastAPI + uvicorn | 0.111.0 | AnaModel chat + tool use |
| Ingress | Traefik | 2.x | TLS termination, path routing |
| Event-driven scaling | KEDA | 2.x | Prometheus-triggered HPA |
| Metrics | Prometheus + kube-prometheus-stack | — | 27 scrape targets, 15d retention |
| Dashboards | Grafana | 13.2.4 | Anonymous access, sidecar provisioning |
| Alerting | AlertManager | — | 6 PrometheusRules |
| Logs | Loki + FluentBit | — | DaemonSet log shipping |
| GPU metrics | NVIDIA DCGM Exporter | 4.4.1 | PodMonitor in gke-managed-system |
| Response cache | Redis 7 | — | AOF persistence, K8s Secret auth |
| Spend database | PostgreSQL 16 | — | LiteLLM spend logs, daily backup |

---

## The Observability Stack — Every Component Explained

### Prometheus
The metrics time-series database. Scrapes every component every 30 seconds. Stores 15 days of data capped at 45 GB. Everything else either feeds into Prometheus or reads from it.

### Grafana (anonymous, no login required)
Reads from Prometheus and renders dashboards. Three custom dashboards:

| Dashboard | Key Panels |
|---|---|
| **vLLM vs SGLang — Engine Comparison** | Queue depth, KV-cache utilization, running requests, token throughput per engine |
| **GPU Utilization (DCGM)** | SM utilization %, VRAM used (GiB), power draw (W), temperature (°C) |
| **LiteLLM Gateway** | Request rate, token throughput, spend rate ($/hr), error rate |

Plus 15 built-in Kubernetes dashboards: API server, Kubelet, Node resources, Namespace resources, Networking, etcd, CoreDNS, and more.

### AlertManager
Six PrometheusRules that fire alerts when thresholds are breached:

| Alert | Condition |
|---|---|
| `VllmGpuCacheNearSaturation` | KV-cache > 95% for 2 min |
| `VllmRequestQueueSpiking` | Queue > 20 requests for 5 min |
| `VllmPodCrashLooping` | Restart count > 3 in 5 min |
| `VllmPodNotReady` | Pod not ready for 3 min |
| `NodeProvisioningStalled` | KEDA active but no new node for 10 min |
| `PrometheusDiskPressure` | TSDB > 90% of 45 GB cap |

### Node Exporter (DaemonSet — 3 pods, one per node)
Exports host-level metrics: CPU, RAM, disk I/O, network bandwidth from each Linux node.

### DCGM Exporter (DaemonSet — GKE-managed, GPU nodes only)
NVIDIA's official GPU metrics exporter. Runs in `gke-managed-system`. Exports SM utilization, VRAM used/free, power draw, temperature, tensor core activity. Wired into Prometheus via a custom PodMonitor.

### Kube State Metrics
Watches the Kubernetes API and exports object state: pod restarts, deployment replica counts, KEDA ScaledObject status, HPA targets.

### Loki + FluentBit (DaemonSet)
FluentBit tails container log files on each node and ships them to Loki. Queryable in Grafana's Explore tab with LogQL. Covers `llm-serving` and `llm-gateway` namespaces.

### Prometheus Operator
Watches for `ServiceMonitor` and `PodMonitor` objects and automatically updates Prometheus scrape configs without restarting.

---

## Models and Engines

| Model | Engine | Alias | Min Replicas | Status |
|---|---|---|---|---|
| Qwen3 8B AWQ | vLLM 0.32.0 | `qwen3-vllm` / `AnaModel` | 1 | **Always running** |
| Qwen3 8B AWQ | SGLang 0.5.20 | `qwen3-sglang` | 0 | Scales on demand |
| Llama 3.1 8B AWQ | vLLM 0.32.0 | `llama3-vllm` | 0 | Scales on demand |
| Llama 3.1 8B AWQ | SGLang 0.5.20 | `llama3-sglang` | 0 | Scales on demand |

Virtual aliases `llama3` and `qwen3` round-robin across both engines. Only `qwen3-vllm` stays always-on — all others are KEDA min=0 and provision on first request.

### AnaModel Identity
The platform exposes Qwen3 8B as **AnaModel** — an AI assistant built by Nexora. A custom LiteLLM callback (`NexoraHandler`) post-processes every response and scrubs references to Qwen, Alibaba, or underlying model identity. Qwen3's RLHF training overrides system prompts for direct identity questions, so the scrub happens at the output layer rather than the input layer.

---

## KEDA — Event-Driven Autoscaling

KEDA replaces CPU-based HPA for GPU inference. GPU pods run at 5–15% CPU even when the GPU is fully saturated — CPU-based scaling never fires. KEDA instead watches:

- `sum(vllm:num_requests_waiting{model_name="..."})` — primary trigger (threshold: 5 waiting)
- `avg(vllm:kv_cache_usage_perc{model_name="..."}) * 100` — secondary trigger (threshold: 80%)

When demand drops to zero, KEDA scales back to 0 replicas, releasing the GPU node. When the first request arrives, the cluster autoscaler provisions a new GPU node and KEDA scales the deployment back up.

---

## Cost (Oct 22 – Oct 27, 5.3 days live)

### Total Spent: $152.82

| Item | Cost | Note |
|---|---|---|
| GPU node (g2-standard-8 × 1 L4) | **$114.61** | 128 hrs × $0.898/hr — 75% of total |
| CPU nodes (e2-standard-4 × 2) | $34.21 | System + monitoring workloads |
| SSD persistent disks (~80 GB) | $0.56 | Prometheus, Grafana, Postgres PVCs |
| Load balancer (Traefik) | $3.19 | Forwarding rule |
| Token billing (LiteLLM) | $0.24 | 1.25M tokens at $0.0002/1k |
| **Total** | **$152.82** | |

| Projection | Amount |
|---|---|
| Daily run rate | $28.74/day |
| Monthly (current setup) | ~$862/month |
| Monthly (2 GPU nodes) | ~$1,510/month |

The GPU node accounts for 75% of all cost. The token billing ($0.24) is negligible because the platform uses self-hosted models — there is no per-token API fee, only the infrastructure running cost.

---

## What We Could Not Achieve

### 1. Second GPU — Blocked by GCP Quota

The most significant limitation. GCP allocates `GPUS_ALL_REGIONS: limit=1` to new projects, meaning only one GPU is available across all regions globally. We hit this ceiling on day one.

**Impact:**
- Only `qwen3-vllm` runs permanently on the single L4
- `llama3-vllm`, `llama3-sglang`, and `qwen3-sglang` are configured and deployed but sit at 0 replicas
- The dual-engine live comparison (vLLM vs SGLang side-by-side) cannot run simultaneously — both engines would require separate GPU nodes
- KEDA correctly detects demand and triggers scale-up for the other deployments, but the cluster autoscaler immediately fails with `GCE quota exceeded`

**What was tried:**
- Spot/preemptible GPU node pool — created and configured, but `GPUS_ALL_REGIONS` counts preemptible GPUs too
- Different GCP regions — every region has L4 limit=1, all blocked by the same global cap
- Self-service quota increase form — submitted, pending Google approval (3–5 business days)

**Resolution:** Waiting for GCP to approve the quota increase from 1 → 4 L4 GPUs in us-central1. Once approved, all four model/engine combinations can run simultaneously.

### 2. Live vLLM vs SGLang Comparison

Because only one GPU is available, a true side-by-side engine comparison under live traffic is not possible in the current state. The Grafana "Engine Comparison" dashboard panels for SGLang show no data (as expected — 0 replicas running).

The architecture is fully wired for it: separate ServiceMonitors, separate KEDA ScaledObjects, separate LiteLLM routes with fallback configured. The comparison will activate automatically once the second GPU is provisioned.

### 3. Llama 3.1 8B Serving

Llama 3.1 8B AWQ is deployed and configured but cannot run with the current single GPU (already occupied by Qwen3). The deployment manifests, LiteLLM config, KEDA ScaledObjects, and ServiceMonitors are all in place — it is a capacity problem, not a configuration problem.

### 4. AWS EKS Migration

The original plan specified AWS EKS as the target infrastructure. GKE was chosen instead because:
- SkyPilot (the original provisioner) had reliability issues on RunPod
- AWS SCP restrictions prevented EKS deployment on the available accounts
- GCP provided a functional free-tier starting point with GKE Autopilot disabled

The Kubernetes manifests are cloud-agnostic and would work on EKS with minor changes (swap `gke-managed-system` DCGM references, update node selectors).

---

## Quick Start

```bash
# 1. Authenticate
gcloud auth login
gcloud config set project testing-494308
gcloud container clusters get-credentials llm-inference --region us-central1

# 2. Verify cluster
kubectl get nodes
kubectl get pods -n llm-serving
kubectl get pods -n llm-gateway
kubectl get pods -n monitoring

# 3. Call the API
curl -X POST https://api.136.65.123.94.nip.io/agent/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-b0454325deddc837764f88240c8f7986" \
  -d '{"model": "AnaModel", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'

# 4. Open the chat UI
open http://136.65.123.94.nip.io/agent/chat

# 5. Open Grafana (no login required)
open http://grafana.136.65.123.94.nip.io
```

---

## Repository Structure

```
k8s/
├── base/                    Namespaces + NetworkPolicies (default-deny egress)
├── vllm/
│   ├── llama3/              Deployment, Service, ServiceMonitor, KEDA ScaledObject
│   └── qwen3/               Deployment, Service, ServiceMonitor, KEDA ScaledObject
├── sglang/
│   ├── llama3/              Deployment, Service, ServiceMonitor, KEDA ScaledObject
│   └── qwen3/               Deployment, Service, ServiceMonitor, KEDA ScaledObject
├── litellm/                 Gateway, Redis, PostgreSQL, HPA, PDB, Ingress
├── agent/                   AnaModel agent server + chat UI
└── monitoring/
    ├── prometheus-rules.yaml  6 AlertManager rules
    ├── dashboards/            3 Grafana dashboard ConfigMaps
    └── dcgm-podmonitor.yaml   DCGM scrape target

helm/
├── kube-prometheus-stack-values.yaml  retentionSize: 45GB, anonymous Grafana access
├── keda-values.yaml
├── traefik-values.yaml
└── loki-stack-values.yaml

provision/
└── eks/                     Original Terraform (AWS EKS — superseded by GKE)

scripts/
├── k8s-deploy.sh            Full platform deployment sequence
├── k8s-load-test.sh         Load test with concurrency sweep
└── k8s-failure-drill.sh     Kill pods + verify recovery

benchmark/
├── production_runner.py     Run lab2 scenarios against live LiteLLM
├── compare.py               Side-by-side lab vs production comparison
└── slo_monitor.py           Poll live Prometheus; alert on SLO breach

docs/
├── architecture.md
├── cost-profile.md
└── runbooks/
    ├── gpu-node-failure.md
    ├── grafana-recovery.md
    └── quota-increase.md
```

---

## Key Design Decisions

### KEDA on Queue Depth, Not CPU
GPU pods consume 5–15% CPU even when the GPU is fully saturated. CPU-based HPA never fires. KEDA watches `vllm:num_requests_waiting` — the number of requests queued inside vLLM waiting for a GPU slot. This is the correct scaling signal for LLM inference.

### LiteLLM as Identity Layer
The gateway does more than route requests. The `NexoraHandler` callback intercepts every response and scrubs model identity from the output text. This is necessary because instruction-tuned models like Qwen3 are fine-tuned to answer identity questions honestly even when a system prompt says otherwise — so scrubbing must happen at the output, not the input.

### DCGM PodMonitor, Not ServiceMonitor
GKE manages the DCGM exporter in `gke-managed-system` without creating a Kubernetes Service for it. The standard `ServiceMonitor` approach fails with no-service pods. A `PodMonitor` targeting the pod directly by label (`app.kubernetes.io/name: gke-managed-dcgm-exporter`) solves this without modifying GKE-managed resources.

### Grafana Sidecar Dashboard Provisioning
Dashboards are stored as ConfigMaps with label `grafana_dashboard=1`. The Grafana sidecar container watches for ConfigMaps with this label across all namespaces and automatically loads them as JSON dashboards. No manual import, no dashboard persistence issues on pod restart.

### startupProbe Over initialDelaySeconds
`startupProbe` with `failureThreshold: 40, periodSeconds: 15` gives a 10-minute budget for vLLM to load model weights. The pod is marked ready exactly when it responds, not after a fixed delay. This is critical on L4 nodes where model loading varies from 2–8 minutes depending on whether weights are cached.

---

## Security

- **NetworkPolicies** — default-deny ingress and egress on all namespaces; explicit allow rules per service
- **Redis** — `requirepass` sourced from K8s Secret; no unauthenticated access
- **PostgreSQL** — password from K8s Secret; not externally exposed
- **LiteLLM master key** — required on every API request; vLLM pods not directly exposed
- **TLS** — all external endpoints served over HTTPS via Traefik + TLS secrets
- **Secrets** — never committed to git; all sourced from K8s Secrets at runtime

---

## Companion Projects

| Project | Description |
|---|---|
| [inference-optimizer-lab2](https://github.com/AyeniOluwatosinOlawale/inference-optimizer-lab2) | Lab: benchmarks vLLM vs SGLang in isolation (TTFT, TPOT, concurrency sweeps) |
| [nexora-platform](https://github.com/AyeniOluwatosinOlawale/nexora-platform) | Product: inference-web UI, gateway API, AnaModel agent, observability dashboard |
| [llm-inference-benchmark](https://github.com/AyeniOluwatosinOlawale/llm-inference-benchmark) | Harness: request generator and latency measurement tool |
