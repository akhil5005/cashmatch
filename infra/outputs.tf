output "url" {
  description = "The review UI."
  value       = "http://${aws_eip.app.public_ip}"
}

output "api_docs" {
  description = "Interactive OpenAPI documentation."
  value       = "http://${aws_eip.app.public_ip}/api/docs"
}

output "public_ip" {
  description = "Static address of the application host."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  description = "For `aws ssm start-session --target <id>`."
  value       = aws_instance.app.id
}

output "db_endpoint" {
  description = "RDS endpoint. Reachable only from the application security group."
  value       = aws_db_instance.main.address
}

output "ecr_api" {
  description = "Repository the API image is pushed to."
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_ui" {
  description = "Repository the UI image is pushed to."
  value       = aws_ecr_repository.ui.repository_url
}

output "region" {
  value = var.region
}
