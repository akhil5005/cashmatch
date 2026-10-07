variable "region" {
  description = "AWS region. Mumbai, because the data is an Indian distributor's book."
  type        = string
  default     = "ap-south-1"
}

variable "owner" {
  description = "Tag applied to every resource, so nothing is orphaned anonymously."
  type        = string
  default     = "akhil"
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "cashmatch"
}

# t3.micro is free-tier eligible and has 1 GB of RAM. That is enough to *run*
# the stack but not to build the frontend, so images are built locally and
# pushed to ECR -- see deploy.sh.
variable "instance_type" {
  description = "EC2 instance type. Free tier covers t3.micro for 750 hours a month."
  type        = string
  default     = "t3.micro"
}

variable "db_instance_class" {
  description = "RDS instance class. Free tier covers db.t3.micro for 750 hours a month."
  type        = string
  default     = "db.t3.micro"
}

variable "db_storage_gb" {
  description = "RDS storage. Free tier covers 20 GB."
  type        = number
  default     = 20
}

variable "llm_mode" {
  description = <<-EOT
    mock (default), live or off. Deployed as `mock` deliberately: the public
    demo should not hold a live API key, and mock mode exercises the entire
    pipeline without one.
  EOT
  type        = string
  default     = "mock"
}
