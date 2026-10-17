output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  value     = module.eks.cluster_certificate_authority_data
  sensitive = true
}

output "kubeconfig_command" {
  description = "Run this to configure kubectl"
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks.cluster_name}"
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "private_subnets" {
  value = module.vpc.private_subnets
}

output "gpu_node_group_arn" {
  value = module.eks.eks_managed_node_groups["gpu"].node_group_arn
}

output "karpenter_controller_role_arn" {
  description = "IAM role ARN for Karpenter controller (injected into helm/karpenter-values.yaml by deploy-karpenter)"
  value       = aws_iam_role.karpenter_controller.arn
}

output "karpenter_node_instance_profile" {
  description = "Instance profile name worn by Karpenter-launched nodes (injected into k8s/karpenter/ec2-node-class.yaml)"
  value       = aws_iam_instance_profile.karpenter_node.name
}

output "karpenter_interruption_queue_name" {
  description = "SQS queue name for Karpenter spot interruption handling"
  value       = aws_sqs_queue.karpenter_interruption.name
}

output "karpenter_interruption_queue_url" {
  description = "SQS queue URL for reference"
  value       = aws_sqs_queue.karpenter_interruption.url
}
