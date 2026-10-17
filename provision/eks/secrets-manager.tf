# ── AWS Secrets Manager + ESO IRSA ────────────────────────────────────────────
# Stores platform secrets in AWS Secrets Manager.
# External Secrets Operator (ESO) syncs them into K8s Secrets automatically,
# replacing the manual `kubectl create secret` approach.
#
# Secrets created here (populated after first terraform apply):
#   /{cluster_name}/platform  → HF token, LiteLLM master key
#   /{cluster_name}/litellm   → Redis password, Postgres password, database URL
#
# ESO reads these via the IRSA role below and writes K8s Secrets.

resource "aws_secretsmanager_secret" "platform" {
  name                    = "/${var.cluster_name}/platform"
  description             = "Platform-level secrets: HuggingFace token, LiteLLM master key"
  recovery_window_in_days = 7
  tags                    = { Project = "llm-inference" }
}

resource "aws_secretsmanager_secret" "litellm" {
  name                    = "/${var.cluster_name}/litellm"
  description             = "LiteLLM gateway secrets: Redis password, Postgres password, database URL"
  recovery_window_in_days = 7
  tags                    = { Project = "llm-inference" }
}

# Placeholder versions — real values are written by scripts/generate-secrets.sh
# via: aws secretsmanager put-secret-value --secret-id ... --secret-string ...
resource "aws_secretsmanager_secret_version" "platform" {
  secret_id = aws_secretsmanager_secret.platform.id
  secret_string = jsonencode({
    HUGGINGFACE_TOKEN   = "REPLACE_AFTER_APPLY"
    LITELLM_MASTER_KEY  = "REPLACE_AFTER_APPLY"
    GRAFANA_ADMIN_PASSWORD = "REPLACE_AFTER_APPLY"
  })

  lifecycle {
    # Prevent Terraform from overwriting values written by generate-secrets.sh
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret_version" "litellm" {
  secret_id = aws_secretsmanager_secret.litellm.id
  secret_string = jsonencode({
    REDIS_PASSWORD    = "REPLACE_AFTER_APPLY"
    POSTGRES_PASSWORD = "REPLACE_AFTER_APPLY"
    DATABASE_URL      = "REPLACE_AFTER_APPLY"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# ── IRSA role for External Secrets Operator ────────────────────────────────────

data "aws_iam_policy_document" "eso_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:external-secrets:external-secrets"]
    }
  }
}

resource "aws_iam_role" "eso" {
  name               = "${var.cluster_name}-eso"
  assume_role_policy = data.aws_iam_policy_document.eso_assume.json
  tags               = { Project = "llm-inference" }
}

resource "aws_iam_role_policy" "eso_secrets_access" {
  name = "${var.cluster_name}-eso-secrets"
  role = aws_iam_role.eso.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
          "secretsmanager:ListSecretVersionIds",
        ]
        Resource = [
          aws_secretsmanager_secret.platform.arn,
          aws_secretsmanager_secret.litellm.arn,
        ]
      }
    ]
  })
}

output "eso_role_arn" {
  description = "IRSA role ARN for External Secrets Operator"
  value       = aws_iam_role.eso.arn
}

output "platform_secret_arn" {
  description = "AWS Secrets Manager ARN for platform secrets"
  value       = aws_secretsmanager_secret.platform.arn
}

output "litellm_secret_arn" {
  description = "AWS Secrets Manager ARN for LiteLLM secrets"
  value       = aws_secretsmanager_secret.litellm.arn
}
