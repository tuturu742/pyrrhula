output "url" {
  description = "The product. Open it, sign up, go."
  value       = "http://${aws_lb.this.dns_name}"
}

# The generated password is never printed: it is in Secrets Manager, and this says how to
# read it rather than putting it in terraform's output (and therefore in state and CI logs).
output "platform_admin_signin" {
  description = "How to sign in as the platform admin created on first boot."
  value = join(" ", [
    "organization 'admin', email '${var.admin_email}';",
    "password: aws secretsmanager get-secret-value --secret-id ${var.name}/admin-password",
    "--query SecretString --output text",
  ])
}

output "admin_url" {
  description = "Platform-admin console (only if admin_cidrs was set)."
  value       = length(var.admin_cidrs) > 0 ? "http://${aws_lb.this.dns_name}:8100" : "(disabled -- set admin_cidrs)"
}

output "ecr_app" {
  value = aws_ecr_repository.app.repository_url
}

output "ecr_web" {
  value = aws_ecr_repository.web.repository_url
}

output "ecr_searxng" {
  value = aws_ecr_repository.searxng.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.this.name
}

output "region" {
  value = var.region
}

output "migrate_task_family" {
  value = aws_ecs_task_definition.migrate.family
}

# Consumed by migrate.sh (aws ecs run-task needs a network configuration).
output "subnets" {
  value = aws_subnet.public[*].id
}

output "app_security_group" {
  value = aws_security_group.app.id
}

output "private_subnet_note" {
  value = "Demo topology: public subnets + SG isolation, no NAT. See README for hardening."
}

output "next_steps" {
  value = <<-EOT
    1. ./build-and-push.sh          # build both images, push to ECR
    2. ./migrate.sh                 # run schema migrations (first boot: also seeds roles)
    3. open the url output above, Sign up, and you're in
    Admin token / DB password / encryption key live in Secrets Manager under "${var.name}/".
  EOT
}
