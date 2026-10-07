# ---------------------------------------------------------------------------
# Database, container registry, and secrets
# ---------------------------------------------------------------------------

# A password nobody ever types, sees, or commits. Terraform generates it,
# writes it to SSM as a SecureString, and the instance reads it at boot
# through its IAM role. It never appears in a file on disk -- including the
# compose file, which references the environment rather than a literal.
resource "random_password" "db" {
  length  = 32
  special = true
  # RDS rejects these in a master password.
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_instance" "main" {
  identifier     = "${var.name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage = var.db_storage_gb
  storage_type      = "gp2"
  storage_encrypted = true

  db_name  = "cashmatch"
  username = "cashmatch"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]

  # The whole point of the private subnets. There is no route from the
  # internet to this instance and no public endpoint to find.
  publicly_accessible = false

  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true

  # Free tier is single-AZ. Multi-AZ is one boolean away when it matters.
  multi_az = false

  tags = { Name = "${var.name}-db" }
}

# --- container registry -----------------------------------------------------

# t3.micro has 1 GB of RAM, which is enough to run the stack and nowhere near
# enough to build the frontend. Images are built on a developer machine (or
# CI) and pulled here, which also means a deploy is a pull rather than a
# build -- fast, and identical to what was tested.
resource "aws_ecr_repository" "api" {
  name                 = "${var.name}/api"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "ui" {
  name                 = "${var.name}/ui"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ECR's free tier is 500 MB. The API image is larger than that, so untagged
# layers are expired rather than accumulating.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after a day"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 1
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "ui" {
  repository = aws_ecr_repository.ui.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after a day"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 1
      }
      action = { type = "expire" }
    }]
  })
}

# --- secrets ----------------------------------------------------------------

# SSM Parameter Store rather than a .env file on the box. The instance reads
# these through its IAM role at boot; rotating a value is a parameter update
# and a restart, not an edit on a server somebody has to SSH into.
resource "aws_ssm_parameter" "database_url" {
  name  = "/${var.name}/database_url"
  type  = "SecureString"
  value = "postgresql+psycopg://${aws_db_instance.main.username}:${urlencode(random_password.db.result)}@${aws_db_instance.main.address}:5432/${aws_db_instance.main.db_name}"

  tags = { Name = "${var.name}-database-url" }
}

# A placeholder on purpose. The deployment runs in LLM_MODE=mock, which
# exercises the whole extraction pipeline with no key and no network. A
# public demo has no business holding a live API key.
resource "aws_ssm_parameter" "gemini_api_key" {
  name  = "/${var.name}/gemini_api_key"
  type  = "SecureString"
  value = "unset-deployment-runs-in-mock-mode"

  lifecycle {
    # So that setting a real key by hand later is not reverted by the next
    # terraform apply.
    ignore_changes = [value]
  }

  tags = { Name = "${var.name}-gemini-api-key" }
}
