# ── S3 backup bucket for pg_dump (LiteLLM spend database) ─────────────────────
# The backup CronJob in k8s/litellm/postgres.yaml pipes pg_dump output here.
# Replaces the previous local-PVC approach which lost backups on node eviction.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "pg_backup" {
  bucket        = "${var.cluster_name}-pg-backup-${data.aws_caller_identity.current.account_id}"
  force_destroy = false

  tags = {
    Project = "llm-inference"
    Purpose = "litellm-postgres-backups"
  }
}

resource "aws_s3_bucket_versioning" "pg_backup" {
  bucket = aws_s3_bucket.pg_backup.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pg_backup" {
  bucket = aws_s3_bucket.pg_backup.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

# Keep 30 daily backups, then expire.
resource "aws_s3_bucket_lifecycle_configuration" "pg_backup" {
  bucket = aws_s3_bucket.pg_backup.id

  rule {
    id     = "expire-old-backups"
    status = "Enabled"
    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_public_access_block" "pg_backup" {
  bucket                  = aws_s3_bucket.pg_backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── IRSA for the backup CronJob pod ───────────────────────────────────────────
# The CronJob runs as a K8s ServiceAccount (llm-gateway/postgres-backup).
# This role allows it to PutObject to the backup bucket only.

data "aws_iam_policy_document" "pg_backup_assume" {
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
      values   = ["system:serviceaccount:llm-gateway:postgres-backup"]
    }
  }
}

resource "aws_iam_role" "pg_backup" {
  name               = "${var.cluster_name}-pg-backup"
  assume_role_policy = data.aws_iam_policy_document.pg_backup_assume.json
  tags               = { Project = "llm-inference" }
}

resource "aws_iam_role_policy" "pg_backup" {
  name = "s3-put-backup"
  role = aws_iam_role.pg_backup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PutBackups"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.pg_backup.arn}/*"
      },
      {
        Sid      = "ListBackups"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.pg_backup.arn
      }
    ]
  })
}

output "pg_backup_bucket" {
  description = "S3 bucket name for pg_dump backups (set in k8s/litellm/postgres.yaml CronJob)"
  value       = aws_s3_bucket.pg_backup.bucket
}

output "pg_backup_role_arn" {
  description = "IAM role ARN for the postgres-backup ServiceAccount (IRSA)"
  value       = aws_iam_role.pg_backup.arn
}
