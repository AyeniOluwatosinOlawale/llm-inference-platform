###############################################################################
# RDS PostgreSQL — LiteLLM Spend Database
###############################################################################

###############################################################################
# RDS PostgreSQL — LiteLLM Spend Database (Multi-AZ, HA)
###############################################################################
# Password

resource "random_password" "litellm_db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

###############################################################################
# Subnet Group
###############################################################################

resource "aws_db_subnet_group" "litellm" {
  name        = "${var.cluster_name}-litellm"
  description = "Private subnet group for LiteLLM RDS instance"
  subnet_ids  = module.vpc.private_subnets

  tags = {
    Name    = "${var.cluster_name}-litellm-subnet-group"
    Project = "llm-inference"
  }
}

###############################################################################
# Security Group
###############################################################################

resource "aws_security_group" "litellm_rds" {
  name        = "${var.cluster_name}-litellm-rds"
  description = "Allow PostgreSQL inbound from EKS nodes only"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "PostgreSQL from EKS node security group"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.cluster_name}-litellm-rds-sg"
    Project = "llm-inference"
  }
}

###############################################################################
# RDS Instance
###############################################################################

resource "aws_db_instance" "litellm" {
  identifier = "${var.cluster_name}-litellm"

  # Engine
  engine         = "postgres"
  engine_version = "16"

  # Sizing
  instance_class        = "db.t3.medium"
  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"

  # Database / credentials
  db_name  = "litellm"
  username = "litellm"
  password = random_password.litellm_db.result

  # Network
  db_subnet_group_name   = aws_db_subnet_group.litellm.name
  vpc_security_group_ids = [aws_security_group.litellm_rds.id]
  publicly_accessible    = false

  # High availability
  multi_az = true

  # Storage encryption
  storage_encrypted = true

  # Backups
  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "Mon:04:00-Mon:05:00"

  # Deletion / snapshot protection
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.cluster_name}-litellm-final"

  # Performance Insights
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  # Parameter / option groups — use defaults for Postgres 16
  auto_minor_version_upgrade = true

  tags = {
    Name    = "${var.cluster_name}-litellm"
    Project = "llm-inference"
  }
}

###############################################################################
# Outputs
###############################################################################

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint (host:port) for LiteLLM DATABASE_URL"
  value       = aws_db_instance.litellm.endpoint
}

output "rds_password" {
  description = "RDS master password for LiteLLM (sensitive)"
  value       = random_password.litellm_db.result
  sensitive   = true
}
