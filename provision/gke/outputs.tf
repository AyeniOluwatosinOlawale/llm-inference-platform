output "cluster_name" {
  value = google_container_cluster.main.name
}

output "cluster_endpoint" {
  value     = google_container_cluster.main.endpoint
  sensitive = true
}

output "kubeconfig_command" {
  description = "Run this to configure kubectl"
  value       = "gcloud container clusters get-credentials ${var.cluster_name} --region ${var.region} --project ${var.project_id}"
}

output "cpu_node_pool_id" {
  value = google_container_node_pool.cpu.id
}

output "gpu_node_pool_id" {
  value = google_container_node_pool.gpu.id
}
