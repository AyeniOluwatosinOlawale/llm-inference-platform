terraform {
  required_version = ">= 1.5.0"

  required_providers {
    runpod = {
      source  = "runpod-infra/runpod"
      version = "~> 0.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
  }

  # FIX #3: Remote state — no local-only tfstate files.
  # Pre-requisite: create the S3 bucket and DynamoDB table manually (one-time):
  #   aws s3 mb s3://llm-inference-platform-tfstate --region us-east-1
  #   aws dynamodb create-table --table-name llm-inference-platform-tflock \
  #     --attribute-definitions AttributeName=LockID,AttributeType=S \
  #     --key-schema AttributeName=LockID,KeyType=HASH \
  #     --billing-mode PAY_PER_REQUEST --region us-east-1
  backend "s3" {
    bucket         = "llm-inference-platform-tfstate"
    key            = "runpod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "llm-inference-platform-tflock"
    encrypt        = true
  }
}
