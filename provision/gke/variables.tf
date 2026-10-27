variable "project_id" {
  description = "GCP project ID — found in the Google Cloud Console dashboard"
  type        = string
}

variable "region" {
  description = "GCP region — europe-west4 (Netherlands) has T4/A100 GPU availability"
  type        = string
  default     = "europe-west4"
}

variable "cluster_name" {
  type    = string
  default = "llm-inference"
}

variable "kubernetes_version" {
  description = "GKE release channel version prefix — leave empty to use STABLE channel default"
  type        = string
  default     = "1.30"
}

variable "cpu_machine_type" {
  description = "Machine type for CPU node pool"
  type        = string
  default     = "e2-standard-4" # 4 vCPU, 16GB RAM
}

variable "cpu_node_count" {
  description = "Number of CPU nodes (per zone)"
  type        = number
  default     = 2
}

variable "cpu_disk_size_gb" {
  type    = number
  default = 100
}

variable "gpu_machine_type" {
  description = "Machine type for GPU node pool"
  type        = string
  default     = "n1-standard-4" # required for T4 attachment
}

variable "gpu_accelerator_type" {
  description = "GPU type — nvidia-tesla-t4 (16GB) is cheapest and available in europe-west4"
  type        = string
  default     = "nvidia-tesla-t4"
}

variable "gpu_node_count" {
  description = "Number of GPU nodes. Set 0 to start, scale up when needed."
  type        = number
  default     = 0
}

variable "gpu_disk_size_gb" {
  type    = number
  default = 200
}
