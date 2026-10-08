# null until the distribution exists, which it does not while enable_cdn is
# false. The URLs below key off this single value rather than each repeating
# the conditional, so one place knows how the site is currently reached.
locals {
  cdn_domain = one(aws_cloudfront_distribution.app[*].domain_name)
  base_url   = local.cdn_domain != null ? "https://${local.cdn_domain}" : "http://${aws_eip.app.public_ip}"
}

output "url" {
  description = "The review UI. HTTPS via CloudFront when enabled, plain HTTP on the Elastic IP otherwise."
  value       = local.base_url
}

output "api_docs" {
  description = "Interactive OpenAPI documentation."
  value       = "${local.base_url}/api/docs"
}

output "cdn_domain" {
  description = "CloudFront distribution domain, or null while the CDN is disabled."
  value       = local.cdn_domain
}

output "public_ip" {
  description = <<-EOT
    Address of the application host. With the CDN enabled this is not
    publicly reachable -- the security group admits port 80 from CloudFront's
    prefix list only, and nginx rejects anything without the shared origin
    secret -- so it is for diagnosis rather than browsing. With the CDN off
    it is how the site is served.
  EOT
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
