#!/bin/bash
# Run ONCE before terraform init to create the S3 + DynamoDB remote state backend.
# Requires: aws cli configured, region us-east-1

set -euo pipefail

BUCKET="llm-infra-tfstate-208805233165"
TABLE="llm-infra-tfstate-lock"
REGION="us-east-1"

echo "==> Creating S3 state bucket: $BUCKET"
aws s3api create-bucket \
  --bucket "$BUCKET" \
  --region "$REGION" \
  2>/dev/null || echo "  (bucket already exists)"

aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration '{
    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
  }'

echo "==> Creating DynamoDB lock table: $TABLE"
aws dynamodb create-table \
  --table-name "$TABLE" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" \
  2>/dev/null || echo "  (table already exists)"

echo "==> Done. Now run:"
echo "    cd provision/eks"
echo "    terraform init"
echo "    terraform plan"
echo "    terraform apply"
