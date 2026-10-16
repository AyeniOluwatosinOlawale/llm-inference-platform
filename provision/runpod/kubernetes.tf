# FIX #2: Secrets encryption at rest via KMS — RunPod K8s supports envelope encryption.
resource "runpod_kubernetes_cluster" "main" {
  name    = "${var.project}-cluster"
  version = "1.30"

  # FIX #10: API endpoint is private by default on RunPod K8s.
  # No equivalent of EKS endpoint_public_access = true footgun.

  # FIX #2: KMS envelope encryption for Kubernetes Secrets in etcd.
  encryption_config = {
    enabled    = true
    kms_key_id = runpod_kms_key.etcd.id
  }

  node_groups = [
    {
      name          = "cpu-system"
      node_type     = "CPU"
      min_nodes     = 1
      max_nodes     = 3
      labels        = { "role" = "system" }
    },
    {
      name          = "gpu-serving"
      node_type     = var.gpu_type
      min_nodes     = 1
      max_nodes     = var.gpu_count * 2
      labels        = {
        "role"                     = "gpu-serving"
        "nvidia.com/gpu.present"   = "true"
      }
      taints = [
        {
          key    = "nvidia.com/gpu"
          value  = "present"
          effect = "NoSchedule"
        }
      ]
    }
  ]

  tags = local.common_tags
}

# KMS key for etcd secret encryption.
resource "runpod_kms_key" "etcd" {
  description = "${var.project} etcd secrets encryption key"
  tags        = local.common_tags
}

# Platform secrets stored in Kubernetes — encrypted at rest via KMS above.
resource "kubernetes_secret" "platform" {
  metadata {
    name      = "platform-secrets"
    namespace = "default"
  }

  data = {
    HUGGINGFACE_TOKEN  = var.huggingface_token
    LITELLM_MASTER_KEY = var.litellm_master_key
    REDIS_PASSWORD     = var.redis_password
    POSTGRES_PASSWORD  = var.postgres_password
    GRAFANA_PASSWORD   = var.grafana_admin_password
  }

  type = "Opaque"
}
