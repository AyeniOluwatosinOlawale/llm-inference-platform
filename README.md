# Production LLM Inference Platform on AWS EKS

![EKS](https://img.shields.io/badge/EKS-1.30-FF9900?logo=amazon-eks&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-1.6+-623CE4?logo=terraform&logoColor=white)
![vLLM](https://img.shields.io/badge/vLLM-0.30.0-76B900?logo=nvidia&logoColor=white)
![SGLang](https://img.shields.io/badge/SGLang-0.5.20-76B900?logo=nvidia&logoColor=white)
![KEDA](https://img.shields.io/badge/KEDA-2.x-326CE5?logo=kubernetes&logoColor=white)
![Karpenter](https://img.shields.io/badge/Karpenter-1.0-326CE5?logo=kubernetes&logoColor=white)
![GPU](https://img.shields.io/badge/GPU-A10G%2024GB-76B900?logo=nvidia&logoColor=white)

A fully Terraform-managed platform for serving open-source large language models at production scale on AWS EKS. The platform provisions an EKS 1.30 cluster with GPU node groups, runs **Llama 3.1 8B** and **Qwen3 8B** through both **vLLM v0.30.0** and **SGLang v0.5.20** simultaneously, and routes all traffic through a **LiteLLM** proxy gateway — giving any client a single OpenAI-compatible endpoint with model routing, rate limiting, cost tracking, and response caching.

The goal: demonstrate that two inference engines can run side-by-side in production, enabling live benchmarking, automatic fallback, and direct comparison of vLLM vs SGLang under real traffic.

Companion to [inference-optimizer-lab2](https://github.com/AyeniOluwatosinOlawale/inference-optimizer-lab2): the lab measures how inference engines behave in isolation at different concurrency levels; this platform provisions and operates the infrastructure to serve a real production workload and validate those measurements under live traffic.

| Lab (inference-optimizer-lab2) | This platform |
|---|---|
| Measure TTFT and TPOT | Monitor live request latency per engine |
| Sweep concurrency 1→128 | Scale serving replicas as demand changes |
| Compare vLLM vs SGLang | Serve both engines in production, compare live |
| Define latency SLOs | AlertManager fires when p95 TTFT breaches threshold |

---

## Architecture

```
Client
   │
   ▼
[Traefik Ingress + TLS]
   │
   ▼
[LiteLLM Proxy]  ──  Redis (response cache, AOF persistence)
   │              ──  PostgreSQL (spend tracking, daily backup)
   │
   ├── llama3-vllm    ──▶  [vLLM v0.30.0:   Llama 3.1 8B AWQ]   × KEDA-scaled
   ├── llama3-sglang  ──▶  [SGLang v0.5.20: Llama 3.1 8B AWQ]   × KEDA-scaled
   ├── qwen3-vllm     ──▶  [vLLM v0.30.0:   Qwen3 8B fp8]        × KEDA-scaled
   └── qwen3-sglang   ──▶  [SGLang v0.5.20: Qwen3 8B fp8]        × KEDA-scaled
                                    │
                           [DCGM GPU Exporter]
                                    │
                  [Prometheus + Grafana + Loki + AlertManager → Slack]
```

Virtual aliases `llama3` and `qwen3` round-robin across both engines for live A/B traffic splitting. Direct aliases (`llama3-vllm`, `llama3-sglang`) allow pinning to a specific engine.

---

## What This Project Demonstrates

| Capability | Implementation |
|---|---|
| **Dual-engine serving** | vLLM v0.30.0 + SGLang v0.5.20 running simultaneously on the same cluster |
| **Live A/B comparison** | LiteLLM round-robins `llama3`/`qwen3` across both engines; TTFT visible per-engine in Grafana |
| **API gateway** | LiteLLM: unified routing, rate limiting, cost tracking, Redis caching |
| **Multi-model serving** | Llama 3.1 8B + Qwen3 8B behind a single endpoint |
| **Request-driven scaling** | KEDA ScaledObjects keyed on `num_requests_waiting` per model per engine |
| **Fast GPU node provisioning** | Karpenter provisions g5.xlarge nodes in ~90s (vs ~5 min for managed node groups); spot-first with on-demand fallback |
| **Spot interruption handling** | SQS + EventBridge; Karpenter drains the node in the 2-minute window before EC2 reclaims it |
| **Automatic fallback** | LiteLLM falls back vLLM → SGLang (and vice versa) on engine failure |
| **Secrets encrypted at rest** | KMS CMK encrypts etcd secrets in EKS |
| **Remote Terraform state** | S3 + DynamoDB backend; no local state |
| **Per-node NVMe model cache** | init container checks `/var/lib/models` before downloading; cold start ~30s not ~8 min |
| **6 PrometheusRules** | GPU cache saturation, queue spikes, pod crash loops, pod not-ready, node stall, Prometheus disk pressure |
| **Egress NetworkPolicies** | Default-deny on all namespaces; explicit allowlist for DNS, HuggingFace HTTPS, inter-service traffic |
| **Log aggregation** | Loki + FluentBit DaemonSet across all serving and gateway pods |
| **Slack alerting** | AlertManager fires to `#llm-platform-alerts` on any PrometheusRule breach |
| **Benchmark bridge** | `benchmark/production_runner.py` runs lab2 scenarios against live LiteLLM; `compare.py` produces side-by-side table |

---

## Technology Stack

| Layer | Technology | Version |
|---|---|---|
| Infrastructure | Terraform | ~1.6 |
| Cloud | AWS (EKS, EBS, IAM, VPC) | Provider ~5.0 |
| Kubernetes | Amazon EKS | 1.30 |
| LLM Serving (engine 1) | vLLM | 0.30.0 |
| LLM Serving (engine 2) | SGLang | 0.5.20 |
| Models | Llama 3.1 8B Instruct · Qwen3 8B Instruct | — |
| LLM Gateway | LiteLLM Proxy | latest |
| Event-driven Scaling | KEDA | 2.x |
| GPU Node Autoprovisioning | Karpenter | 1.0.x |
| Ingress | Traefik | latest |
| TLS | cert-manager + Let's Encrypt | latest |
| Metrics | kube-prometheus-stack (Prometheus + Grafana + AlertManager) | latest |
| Log Aggregation | Loki + FluentBit | latest |
| Gateway Cache | Redis 7 (AOF persistence, requirepass auth) | — |
| Gateway Database | PostgreSQL 16 (daily pg_dump, 7-day retention) | — |
| GPU Metrics | NVIDIA DCGM Exporter | — |

---

## Models & Engines

| Model | Engine | Quantization | VRAM | LiteLLM Alias |
|---|---|---|---|---|
| Llama 3.1 8B Instruct | vLLM v0.30.0 | AWQ 4-bit | ~10 GB | `llama3-vllm` |
| Llama 3.1 8B Instruct | SGLang v0.5.20 | AWQ 4-bit | ~10 GB | `llama3-sglang` |
| Qwen3 8B Instruct | vLLM v0.30.0 | fp8 | ~8 GB | `qwen3-vllm` |
| Qwen3 8B Instruct | SGLang v0.5.20 | fp8 | ~8 GB | `qwen3-sglang` |
| _(round-robin)_ | vLLM + SGLang | — | — | `llama3` |
| _(round-robin)_ | vLLM + SGLang | — | — | `qwen3` |

---

## Quick Start

```bash
# 1. Bootstrap remote state (once)
cd provision/eks && bash bootstrap.sh

# 2. Provision cluster
terraform init && terraform apply
aws eks update-kubeconfig --region us-east-1 --name llm-inference

# 3. Generate and configure secrets
make secrets
# Edit .env.secrets: fill HUGGINGFACE_TOKEN, SLACK_WEBHOOK_URL

# 4. Deploy platform (in order)
make deploy-base        # Namespaces + NetworkPolicies
make deploy-karpenter   # Karpenter controller + GPU NodePool (spot-first, ~90s node cold-start)
make deploy-infra       # Prometheus, KEDA, Traefik, Loki (Helm)
make verify-metrics     # Confirm vLLM/SGLang metric labels before KEDA wires up
make deploy-vllm        # Llama3 + Qwen3 on vLLM
make deploy-sglang      # Llama3 + Qwen3 on SGLang
make deploy-litellm     # LiteLLM gateway + Redis + Postgres
make deploy-dashboards  # Grafana dashboards

# 5. Verify
make health-check
make load-test

# 6. Compare engines
make benchmark-prod     # Run lab2 scenarios against live platform
make benchmark-compare LAB=path/to/lab_results.json PROD=path/to/prod_results.json
```

### Calling the API

```bash
# Route to a specific engine
curl https://<your-domain>/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3-vllm", "messages": [{"role": "user", "content": "Hello"}]}'

# Round-robin across both engines automatically
curl https://<your-domain>/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3", "messages": [{"role": "user", "content": "Hello"}]}'
```

---

## Design Decisions and Tradeoffs

### 1. Two inference engines (vLLM + SGLang) instead of one

**Decision:** Run vLLM v0.30.0 and SGLang v0.5.20 simultaneously for every model.

**Why:** The most common question in open-source LLM serving is "which engine should I use?" This platform answers it with live production data instead of synthetic benchmarks. Both engines serve the same models behind the same LiteLLM gateway. Grafana shows per-engine TTFT, TPOT, and throughput in real time.

SGLang v0.5.20 has shown significantly higher throughput than vLLM on certain workloads — running both lets you observe this difference under your actual traffic pattern, not a lab scenario.

**Tradeoff:** Four GPU pods instead of two, doubling GPU cost when both models run on both engines. For cost-sensitive deployments, run one engine per model and use the benchmark results to choose the better one.

---

### 2. LiteLLM as the API gateway with automatic fallback

**Decision:** Route all traffic through LiteLLM with `fallbacks` configured between engines.

**Why:** If vLLM crashes or restarts, LiteLLM automatically reroutes `llama3-vllm` requests to `llama3-sglang` within the same retry cycle. No client change required. This makes the dual-engine setup useful for availability, not just benchmarking.

```yaml
fallbacks:
  - llama3-vllm:   ["llama3-sglang"]
  - llama3-sglang: ["llama3-vllm"]
```

**Tradeoff:** The gateway adds one network hop (~1–5ms overhead). Negligible at LLM inference timescales (500ms–30s).

---

### 3. KEDA on queue depth, not CPU

**Decision:** KEDA ScaledObjects watch `vllm:num_requests_waiting` and `num_queue_reqs` (SGLang), not CPU or memory.

**Why:** GPU inference pods run at 5–15% CPU even when the GPU is fully saturated. CPU-based HPA never fires. The correct signal is the inference engine's internal request queue — when more than 5 requests are waiting for a GPU slot, a second replica is needed.

Each model has independent ScaledObjects for vLLM and SGLang, so they scale separately based on their own queue depth.

---

### 4. Per-node NVMe model cache over shared EFS

**Decision:** Store model weights in `/var/lib/models` on the node's local NVMe disk using a `hostPath` volume, not a shared EFS filesystem.

**Why:** EFS adds ~2–5ms of latency per read at model load time and introduces a dependency on NFS availability. g5.xlarge instances have 250 GB of local NVMe. An init container checks if the weights are already cached before downloading — repeat starts take ~30 seconds instead of ~8 minutes.

**Tradeoff:** Each node downloads independently on first boot. If you scale from 1 to 4 GPU replicas simultaneously, 3 nodes pay the download cost at once. EFS eliminates this by sharing weights across all replicas from a single download. For this platform (max 4 replicas per model), the NVMe approach is simpler and faster in the steady state.

---

### 5. startupProbe over initialDelaySeconds

**Decision:** Use `startupProbe` with `failureThreshold: 40, periodSeconds: 15` (10-minute budget) instead of `initialDelaySeconds`.

**Why:** `initialDelaySeconds` is a fixed wait — if the model loads in 3 minutes but you set 5 minutes, you waste 2 minutes per pod start. If loading takes longer on a cold node, the pod is killed. `startupProbe` polls until the `/health` endpoint responds, then hands off to `readinessProbe`. The pod is marked ready exactly when it is ready.

---

### 6. Karpenter for GPU node provisioning (spot-first, ~90s cold-start)

**Decision:** Replace the static GPU managed node group with Karpenter v1.0.

**Why:** EKS managed node groups provision new nodes via the EC2 Auto Scaling API, which takes 4–6 minutes. Karpenter uses EC2 Fleet's direct launch path: it calls `RunInstances` directly after choosing the optimal AZ and capacity type, cutting cold-start to ~90 seconds. For KEDA-driven scale-up events (burst traffic), this difference determines whether users see a brief queue spike or a multi-minute timeout.

Karpenter also handles spot instance selection automatically. The `NodePool` specifies `capacity-type: [spot, on-demand]`; Karpenter picks the cheapest available spot pool across all three AZs, then falls back to on-demand if no spot capacity exists. g5.xlarge spot runs ~70% cheaper than on-demand ($0.30/hr vs ~$1.01/hr).

Spot interruption is handled via SQS + EventBridge: when EC2 sends a 2-minute interruption notice, the event lands in the SQS queue and Karpenter cordons + drains the node, allowing KEDA to trigger a new replica on a fresh node before the old one terminates.

**Tradeoff:** Karpenter requires an IRSA IAM role with EC2/IAM permissions and must be installed before the first GPU pod is scheduled. The `deploy-karpenter` Makefile target reads the role ARN and queue name from `terraform output` automatically.

---

### 7. postStart warmup wrapped in `|| true`

**Decision:** The postStart warmup hook fires a dummy inference request to pre-compile CUDA kernels, wrapped in `|| true`.

**Why:** CUDA JIT-compiles kernels on the first forward pass. Without warmup, the first real user request absorbs 5–15 seconds of compilation latency. The warmup fires before the pod enters Service endpoints.

The `|| true` is critical: if vLLM is not yet accepting requests when `postStart` fires, the hook returns a non-zero exit code, which kills the container. `|| true` makes the hook best-effort — it compiles kernels when possible but never blocks pod startup.

---

## Infrastructure Layout

```
provision/eks/           Terraform — VPC, KMS, EKS 1.30, CPU managed node group + Karpenter IAM/SQS
k8s/karpenter/           EC2NodeClass (AL2 GPU, 200GB NVMe) + NodePool (g5.xlarge, spot+on-demand)
k8s/base/                Namespaces (llm-serving, llm-gateway, monitoring) + NetworkPolicies
k8s/vllm/llama3/         Deployment, Service, ServiceMonitor, KEDA ScaledObject
k8s/vllm/qwen3/          Deployment, Service, ServiceMonitor, KEDA ScaledObject
k8s/sglang/llama3/       Deployment, Service, ServiceMonitor, KEDA ScaledObject
k8s/sglang/qwen3/        Deployment, Service, ServiceMonitor, KEDA ScaledObject
k8s/litellm/             Gateway, Redis (auth+AOF), Postgres (daily backup), HPA, PDB
k8s/monitoring/          6x PrometheusRules, Loki values, 3x Grafana dashboards (ConfigMaps)
helm/                    kube-prometheus-stack, keda, traefik, loki-stack values
scripts/                 verify-metrics.sh, generate-secrets.sh, health-check.sh, load-test.py
benchmark/               production_runner.py, compare.py, slo_monitor.py
docs/                    Architecture, cost profile, gap-fixes, runbooks
```

---

## Observability

### vLLM Metrics (per model, via ServiceMonitor)

| Metric | What it tells you |
|---|---|
| `vllm:num_requests_waiting` | Queue depth — primary KEDA scale trigger |
| `vllm:gpu_cache_usage_perc` | KV cache pressure — secondary scale trigger at >95% |
| `vllm:num_requests_running` | Active GPU inference slots |
| `vllm:e2e_request_latency_seconds` | End-to-end latency histogram |

### GPU Metrics (DCGM Exporter)

| Metric | What it tells you |
|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | Compute utilisation % |
| `DCGM_FI_DEV_FB_USED` | VRAM used (bytes) |
| `DCGM_FI_DEV_GPU_TEMP` | Temperature — detect thermal throttling |

### AlertManager Rules (6 total → Slack)

| Alert | Condition |
|---|---|
| `VllmGpuCacheNearSaturation` | `gpu_cache_usage > 95%` for 2m |
| `VllmRequestQueueSpiking` | `requests_waiting > 20` for 5m |
| `VllmPodCrashLooping` | restart count > 3 in `llm-serving` for 5m |
| `VllmPodNotReady` | pod not ready in `llm-serving` for 3m |
| `NodeProvisioningStalled` | KEDA active but no allocatable nodes for 10m |
| `PrometheusDiskPressure` | tsdb usage > 90% of 45GB cap |

---

## Security

- **KMS CMK** encrypts all etcd secrets at rest
- **NetworkPolicies** in every namespace — default-deny ingress + egress with explicit allowlists
- **Redis** — `requirepass` auth from K8s Secret; AOF persistence on PVC
- **PostgreSQL** — password from K8s Secret; daily `pg_dump` to PVC
- **Secrets at deploy time** — `.env.secrets` is gitignored; never committed
- **LiteLLM master key** — required on every API request; inference pods not exposed directly

---

## Cost Profile (us-east-1)

| Resource | Type | Est. monthly |
|---|---|---|
| EKS control plane | Managed | ~$73 |
| CPU node (1x m5.xlarge) | On-demand | ~$140 |
| GPU nodes (2x g5.xlarge) | Spot (Karpenter) | ~$435 |
| EBS volumes (Prometheus, Postgres) | gp3 | ~$10 |
| NAT Gateway | Fixed + data | ~$35 |
| S3 (Terraform state) | Standard | ~$1 |
| **Total (full running)** | | **~$694/month** |

> Scale GPU nodes to 0 when not serving traffic. CPU node runs all monitoring and gateway services (~$248/month at idle).

---

## Known Limitations

| Limitation | Notes |
|---|---|
| **4 GPU pods per model** | Each pod uses 1 GPU. 70B+ models require multi-GPU tensor parallelism — not yet implemented. |
| **Per-node model cache** | First pod on a new node downloads weights independently. EFS would share weights across replicas. |
| **PostgreSQL single replica** | No HA for LiteLLM's spend database. Replace with RDS for production durability. |
| **g5.xlarge quota** | New AWS accounts start with 0 vCPUs for G instances. Request quota increase before deploying. |
