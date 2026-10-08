# ---------------------------------------------------------------------------
# The application host
#
# One t3.micro running the two application containers. The database is
# managed by RDS and lives elsewhere, so this box holds no state worth
# keeping -- it can be terminated and recreated from this file at any time,
# which is the property that makes a single instance an acceptable answer
# rather than a pet.
# ---------------------------------------------------------------------------

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
}

# --- identity ---------------------------------------------------------------

resource "aws_iam_role" "app" {
  name = "${var.name}-app-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Shell access through Session Manager: no inbound port, no key pair to lose,
# and every session recorded in CloudTrail.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "ecr" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Read only the three parameters this application owns, and decrypt only with
# the account's default SSM key. Scoped rather than `ssm:*` on `*`, because
# the instance has no business reading anything else in the account.
resource "aws_iam_role_policy" "secrets" {
  name = "${var.name}-read-own-secrets"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = [
          aws_ssm_parameter.database_url.arn,
          aws_ssm_parameter.gemini_api_key.arn,
          aws_ssm_parameter.origin_verify.arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" }
        }
      }
    ]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.name}-app-profile"
  role = aws_iam_role.app.name
}

# --- the instance -----------------------------------------------------------

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  # No key_name. There is nothing to SSH into.

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    region         = var.region
    account_id     = data.aws_caller_identity.current.account_id
    name           = var.name
    api_repository = aws_ecr_repository.api.repository_url
    ui_repository  = aws_ecr_repository.ui.repository_url
    llm_mode       = var.llm_mode
    enable_cdn     = var.enable_cdn ? "true" : "false"
  })

  # Re-run the bootstrap when it changes, rather than leaving a stale box.
  user_data_replace_on_change = true

  depends_on = [aws_db_instance.main]

  tags = { Name = "${var.name}-app" }
}

data "aws_caller_identity" "current" {}

# A static address, so the demo URL survives an instance replacement. Free
# while attached to a running instance; billed only if left dangling, which
# is why it is bound here rather than allocated loose.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = { Name = "${var.name}-eip" }
}
