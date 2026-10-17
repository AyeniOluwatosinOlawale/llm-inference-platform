# ── AWS Budgets — spend alerting ──────────────────────────────────────────────
# Fires an SNS notification (→ email) when monthly spend hits 80% and 100%
# of the expected ~$694/month baseline. Catches runaway GPU scaling early.

resource "aws_sns_topic" "budget_alerts" {
  name = "${var.cluster_name}-budget-alerts"
  tags = { Project = "llm-inference" }
}

resource "aws_sns_topic_subscription" "budget_email" {
  topic_arn = aws_sns_topic.budget_alerts.arn
  protocol  = "email"
  endpoint  = var.budget_alert_email
}

# 80% warning — gives time to investigate before the month closes.
resource "aws_budgets_budget" "monthly_warning" {
  name         = "${var.cluster_name}-monthly-80pct"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd * 0.80)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.budget_alerts.arn]
  }
}

# 100% hard limit — something has gone wrong (runaway spot scale-out, etc.).
resource "aws_budgets_budget" "monthly_hard" {
  name         = "${var.cluster_name}-monthly-100pct"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.budget_alerts.arn]
  }
}

output "budget_sns_topic_arn" {
  description = "SNS topic ARN for budget alerts — confirm the email subscription after terraform apply"
  value       = aws_sns_topic.budget_alerts.arn
}
