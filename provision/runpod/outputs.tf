# FIX #11: Full set of outputs (reference platform only exported 2).

output "cluster_endpoint" {
  description = "RunPod Kubernetes API server endpoint"
  value       = runpod_kubernetes_cluster.main.endpoint
}

output "kubeconfig_path" {
  description = "Write kubeconfig: terraform output -raw kubeconfig > ~/.kube/config"
  value       = runpod_kubernetes_cluster.main.kubeconfig
  sensitive   = true
}

output "cluster_ca_certificate" {
  description = "Base64-encoded cluster CA certificate"
  value       = runpod_kubernetes_cluster.main.cluster_ca_certificate
  sensitive   = true
}

output "network_volume_id" {
  description = "RunPod Network Volume ID for model cache"
  value       = runpod_network_volume.model_cache.id
}

output "kms_key_id" {
  description = "KMS key used for etcd Secrets encryption"
  value       = runpod_kms_key.etcd.id
}

output "litellm_service_url" {
  description = "LiteLLM gateway URL (set after ingress deploy)"
  value       = "https://vllm.${var.dns_zone}"
}

output "grafana_url" {
  description = "Grafana dashboard URL (set after monitoring deploy)"
  value       = "https://grafana.${var.dns_zone}"
}

output "prometheus_url" {
  description = "Prometheus internal URL for KEDA ScaledObject serverAddress"
  value       = "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
}
