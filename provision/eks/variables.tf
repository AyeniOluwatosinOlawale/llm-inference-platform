variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "cluster_name" {
  type    = string
  default = "llm-inference"
}

variable "cluster_version" {
  type    = string
  default = "1.32"
}

variable "gpu_instance_type" {
  description = "g5.xlarge = 1x A10G 24GB VRAM, 4 vCPU, 16 GB RAM (~$1.006/hr on-demand)"
  type        = string
  default     = "g5.xlarge"
}

variable "gpu_node_count" {
  description = "Number of GPU nodes (each runs 1x vLLM or SGLang pod)"
  type        = number
  default     = 2
}

variable "cpu_instance_type" {
  description = "For control-plane services: LiteLLM, KEDA, Prometheus, Grafana"
  type        = string
  default     = "m5.xlarge"
}

variable "cpu_node_count" {
  type    = number
  default = 2
}

variable "budget_alert_email" {
  description = "Email address for AWS Budget spend alerts (must confirm SNS subscription after apply)"
  type        = string
  default     = "ayenioluwatosinolawale@gmail.com"
}

variable "monthly_budget_usd" {
  description = "Expected monthly spend cap in USD — alerts fire at 80% and 100%"
  type        = number
  default     = 700
}
