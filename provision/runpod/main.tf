locals {
  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Owner       = "AyeniOluwatosinOlawale"
  }
}

provider "runpod" {
  api_key = var.runpod_api_key
}

provider "kubernetes" {
  host                   = runpod_kubernetes_cluster.main.endpoint
  cluster_ca_certificate = base64decode(runpod_kubernetes_cluster.main.cluster_ca_certificate)
  token                  = runpod_kubernetes_cluster.main.service_account_token
}

provider "helm" {
  kubernetes {
    host                   = runpod_kubernetes_cluster.main.endpoint
    cluster_ca_certificate = base64decode(runpod_kubernetes_cluster.main.cluster_ca_certificate)
    token                  = runpod_kubernetes_cluster.main.service_account_token
  }
}
