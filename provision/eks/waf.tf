# ── AWS WAF v2 WebACL ─────────────────────────────────────────────────────────
# Sits in front of the ALB (created by AWS LB Controller from k8s/base/alb-ingress.yaml).
# Protects the LiteLLM API from SQL injection, XSS, known bad inputs, and
# brute-force / prompt-injection floods.

resource "aws_wafv2_web_acl" "llm_platform" {
  name  = "${var.cluster_name}-waf"
  scope = "REGIONAL"   # REGIONAL for ALB; CLOUDFRONT would require us-east-1 globally

  default_action {
    allow {}
  }

  # ── AWS Managed Rule Groups ────────────────────────────────────────────────

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 10

    override_action { none {} }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "CommonRuleSet"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 20

    override_action { none {} }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "KnownBadInputs"
      sampled_requests_enabled   = true
    }
  }

  # Blocks IPs on AWS's threat intelligence list (botnets, scanners).
  rule {
    name     = "AWSManagedRulesAmazonIpReputationList"
    priority = 30

    override_action { none {} }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "AmazonIpReputation"
      sampled_requests_enabled   = true
    }
  }

  # ── Rate limiting ──────────────────────────────────────────────────────────
  # LLM inference is expensive — cap each IP at 100 requests per 5-minute window.
  # Prevents runaway clients from exhausting GPU capacity or spend limits.
  rule {
    name     = "RateLimitPerIP"
    priority = 40

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 100
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "RateLimitPerIP"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.cluster_name}-waf"
    sampled_requests_enabled   = true
  }

  tags = {
    Project = "llm-inference"
  }
}

output "waf_acl_arn" {
  description = "WAF WebACL ARN — annotated on the ALB ingress in k8s/base/alb-ingress.yaml"
  value       = aws_wafv2_web_acl.llm_platform.arn
}
