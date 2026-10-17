module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # API endpoint: private + public (restrict public to your IP later)
  cluster_endpoint_public_access = true

  # Encrypt etcd secrets at rest with KMS
  cluster_encryption_config = {
    resources = ["secrets"]
  }

  # Add-ons
  cluster_addons = {
    coredns                = { most_recent = true }
    kube-proxy             = { most_recent = true }
    vpc-cni                = { most_recent = true }
    aws-ebs-csi-driver     = { most_recent = true }
  }

  # ── Node groups ──────────────────────────────────────────────────────────

  eks_managed_node_groups = {
    # GPU nodes: one A10G per node, for vLLM + SGLang pods
    gpu = {
      name           = "gpu-nodes"
      instance_types = [var.gpu_instance_type]
      ami_type       = "AL2_x86_64_GPU"   # Amazon Linux 2 with NVIDIA drivers pre-installed

      min_size     = 0
      max_size     = 4
      desired_size = var.gpu_node_count

      disk_size = 200   # GB; vLLM model cache + container images

      labels = {
        "workload-type" = "gpu"
      }
      taints = [
        {
          key    = "nvidia.com/gpu"
          value  = "true"
          effect = "NO_SCHEDULE"
        }
      ]

    }

    # CPU node: LiteLLM, Redis, Postgres, KEDA, Prometheus, Grafana, Loki
    cpu = {
      name           = "cpu-nodes"
      instance_types = [var.cpu_instance_type]
      ami_type       = "AL2_x86_64"

      min_size     = 1
      max_size     = 3
      desired_size = var.cpu_node_count

      disk_size = 50

      labels = {
        "workload-type" = "cpu"
      }
    }
  }

  # Grant cluster-admin to the IAM identity running Terraform
  enable_cluster_creator_admin_permissions = true

  tags = {
    Project = "llm-inference"
    Env     = "prod"
  }
}
