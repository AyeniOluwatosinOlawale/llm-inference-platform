# Gap Fixes — Reference EKS Platform → This Platform

All 14 gaps identified in the vllm-eks-inference-platform review, mapped to the files that fix them.

| # | Sev | Original Issue | Fix | File(s) |
|---|---|---|---|---|
| 1 | HIGH | KEDA Prometheus query `model_name` label doesn't match `--served-model-name` — autoscaling silently disabled | `verify-metrics.sh` scrapes `/metrics` from live pods, extracts exact label, patches ScaledObjects before apply | `scripts/verify-metrics.sh`, `k8s/vllm/*/scaled-object.yaml`, `k8s/sglang/*/scaled-object.yaml` |
| 2 | HIGH | Kubernetes Secrets unencrypted in etcd | k3s `--secrets-encryption` flag in cloud-init; RunPod K8s: KMS CMK in `kubernetes.tf` | `provision/vastai/cloud-init/server.yaml`, `provision/runpod/kubernetes.tf` |
| 3 | HIGH | No remote Terraform state — local only, single point of failure | RunPod: S3 + DynamoDB backend in `backend.tf`; Vast.ai: `state.py` writes to Supabase Storage | `provision/runpod/backend.tf`, `provision/vastai/state.py` |
| 4 | HIGH | EFS burst-mode throttling on cold model load (default `bursting` mode) | Not applicable — per-node NVMe `hostPath` cache via init container; no shared storage bottleneck | `k8s/vllm/llama3/deployment.yaml` init container |
| 5 | MED | AlertManager running but zero PrometheusRules | 6 alerting rules covering vLLM queue, SGLang queue, KV-cache saturation, pod crash loops, pod not-ready, Prometheus disk pressure | `k8s/monitoring/prometheus-rules.yaml` |
| 6 | MED | No egress NetworkPolicies — compromised pod can reach cloud metadata service | Default-deny-egress on all namespaces; per-namespace explicit egress allowlist (DNS, HuggingFace HTTPS, inter-service) | `k8s/base/network-policies/default-deny.yaml`, `k8s/base/network-policies/egress-allow.yaml` |
| 7 | MED | Redis: no auth, no persistence — cache lost on pod restart causing thundering herd | `requirepass` from K8s Secret; `appendonly yes` AOF persistence in ConfigMap | `k8s/litellm/redis.yaml` |
| 8 | MED | `postStart` warmup hook failure kills container before startupProbe passes; `initialDelaySeconds` too short | Warmup wrapped `|| true` (best-effort); `startupProbe` with `failureThreshold:40, periodSeconds:15` (10 min budget) replaces all `initialDelaySeconds` | `k8s/vllm/*/deployment.yaml`, `k8s/sglang/*/deployment.yaml` |
| 9 | MED | `ignore_changes = [yaml_body]` silently drops all future deployment updates | No `ignore_changes` on manifests; explicit `make redeploy-vllm` / `make redeploy-sglang` targets | `Makefile` |
| 10 | MED | EKS API endpoint public by default (`endpoint_public_access = true`) | RunPod K8s API is private by default; k3s on Vast.ai binds to private IP | `provision/runpod/kubernetes.tf`, `provision/vastai/cloud-init/server.yaml` |
| 11 | LOW | Only 2 Terraform outputs (`vpc_id`, `subnet_ids`) | 8 outputs: `cluster_endpoint`, `kubeconfig_path`, `network_volume_id`, `kms_key_id`, `litellm_service_url`, `grafana_url`, `prometheus_url`, `node_pool_id` | `provision/runpod/outputs.tf` |
| 12 | LOW | No Prometheus `retentionSize` cap — high-cardinality GPU metrics can fill 50 Gi before 15-day window | `retentionSize: "45GB"` added alongside `retention: 15d` | `helm/kube-prometheus-stack-values.yaml` |
| 13 | LOW | Grafana dashboards must be built manually — not reproducible | 3 dashboards (engine comparison, GPU utilization, gateway health) provisioned via Grafana sidecar ConfigMap | `k8s/monitoring/dashboards/dashboards-configmap.yaml` |
| 14 | LOW | No log aggregation — container logs ephemeral, post-incident debugging blind | Loki stack + FluentBit DaemonSet; drops high-cardinality token-level vLLM debug lines | `k8s/monitoring/loki/values.yaml`, `helm/loki-stack-values.yaml` |

## Additional improvements not in the original 14

| Improvement | File |
|---|---|
| LiteLLM gateway had no autoscaling (2 fixed replicas) | `k8s/litellm/hpa.yaml` |
| SGLang added as second serving engine (vLLM-only before) | `k8s/sglang/` |
| Both engines share the same model weight cache per node | `hostPath: /var/lib/models` in all deployment init containers |
| CPU limits added to all containers (none in reference) | All `deployment.yaml` files |
| Lab2 engine clients reused directly for production benchmarking | `benchmark/production_runner.py` |
