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
    # GPU nodes are provisioned by Karpenter (see k8s/karpenter/).
    # Karpenter replaces the static GPU managed node group, cutting cold-start
    # from ~5 min (EKS managed node groups) to ~90 s (EC2 Fleet direct launch).
    # The node pool is defined in k8s/karpenter/node-pool.yaml.

    # CPU node: Karpenter controller, LiteLLM, Redis, Postgres, KEDA, Prometheus, Grafana, Loki.
    # Must exist before Karpenter is installed — Karpenter itself needs a node to land on.
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
