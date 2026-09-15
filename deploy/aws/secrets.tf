# The secrets, generated once and stored in Secrets Manager; task
# definitions reference them by ARN ("secrets", never "environment"), so they appear
# in no task definition JSON and no console page.

resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "random_password" "app_db" {
  length  = 32
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "admin_token" {
  length  = 48
  special = false
}

# The platform admin's first password. Compose and k8s both generate one and inject it
# (deploy/installers/compose.sh, deploy/k8s/dev-up.sh); AWS did not, so the bootstrap in
# packages/api/admin_bootstrap.py -- which needs BOTH the email and the password -- never
# ran and the only way in was the deprecated standalone token console.
resource "random_password" "admin_password" {
  length  = 32
  special = false
}

# AES-256-GCM data key: exactly 32 random bytes, base64 -- same contract as
# `openssl rand -base64 32` in docs/self-host.md. Losing it orphans every sealed
# credential; treat the Secrets Manager entry as precious.
resource "random_bytes" "encryption_key" {
  length = 32
}

locals {
  secrets = {
    postgres-password = random_password.postgres.result
    app-db-password   = random_password.app_db.result
    jwt-secret        = random_password.jwt.result
    admin-token       = random_password.admin_token.result
    admin-password    = random_password.admin_password.result
    encryption-key    = random_bytes.encryption_key.base64
    database-url      = "postgresql+asyncpg://pyrrhula:${random_password.postgres.result}@${aws_db_instance.this.address}:5432/pyrrhula"
    app-database-url  = "postgresql+asyncpg://pyrrhula_app:${random_password.app_db.result}@${aws_db_instance.this.address}:5432/pyrrhula"
  }
}

resource "aws_secretsmanager_secret" "this" {
  for_each                = local.secrets
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "this" {
  for_each      = local.secrets
  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = each.value
}
