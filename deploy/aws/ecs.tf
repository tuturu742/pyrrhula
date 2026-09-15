resource "aws_ecs_cluster" "this" {
  name = var.name
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.name}/app"
  retention_in_days = 14
}

# One-shot exec environments log here (awslogs stream pyr/work/<task-id>); the worker
# reads the stream back as the delegation's build/test output.
resource "aws_cloudwatch_log_group" "exec_envs" {
  name              = "/${var.name}/exec-envs"
  retention_in_days = 7
}

locals {
  app_image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  web_image = "${aws_ecr_repository.web.repository_url}:${var.image_tag}"
  api_host  = "api.${var.name}.local"
  # What a browser types. The ALB has no custom domain/cert here, so a preview share
  # link is its plain DNS name -- override once a real hostname is fronted.
  public_base_url = "http://${aws_lb.this.dns_name}"

  # Mirrors docker/compose.selfhost.yml's &app_env -- one contract, two deployments.
  app_environment = [
    { name = "PYRRHULA_REDIS_URL", value = "redis://${aws_elasticache_cluster.this.cache_nodes[0].address}:6379/0" },
    { name = "PYRRHULA_AUTH_PROVIDER", value = "local" },
    { name = "PYRRHULA_ISOLATION_MODE", value = "shared" },
    { name = "PYRRHULA_SINGLE_TENANT_UI", value = tostring(var.single_tenant_ui) },
    { name = "PYRRHULA_REQUIRE_ENCRYPTION", value = "1" },
    { name = "PYRRHULA_BLOB_STORE_ROOT", value = "/app/data/blobs" },
    { name = "PYRRHULA_GIT_HTTP_BASE", value = "http://${local.api_host}:8000" },
    # Base of a preview share link as a browser sees it -- the ALB, not the in-VPC api.
    { name = "PYRRHULA_PUBLIC_BASE_URL", value = local.public_base_url },
    { name = "PYRRHULA_ADMIN_EMAIL", value = var.admin_email },
    { name = "PYRRHULA_ASSISTANT_MODEL", value = var.assistant_model },
    { name = "PYRRHULA_ASSISTANT_API_BASE", value = "" },
    { name = "PYRRHULA_WEB_SEARCH_URL", value = "http://searxng.${var.name}.local:8080" },
    { name = "HF_HUB_OFFLINE", value = var.hf_offline },
    { name = "TRANSFORMERS_OFFLINE", value = var.hf_offline },
  ]

  app_secrets = [
    { name = "PYRRHULA_DATABASE_URL", valueFrom = aws_secretsmanager_secret.this["database-url"].arn },
    { name = "PYRRHULA_APP_DATABASE_URL", valueFrom = aws_secretsmanager_secret.this["app-database-url"].arn },
    { name = "PYRRHULA_APP_DB_PASSWORD", valueFrom = aws_secretsmanager_secret.this["app-db-password"].arn },
    { name = "PYRRHULA_JWT_SECRET", valueFrom = aws_secretsmanager_secret.this["jwt-secret"].arn },
    { name = "PYRRHULA_ADMIN_TOKEN", valueFrom = aws_secretsmanager_secret.this["admin-token"].arn },
    { name = "PYRRHULA_ENCRYPTION_KEY", valueFrom = aws_secretsmanager_secret.this["encryption-key"].arn },
    { name = "PYRRHULA_ADMIN_PASSWORD", valueFrom = aws_secretsmanager_secret.this["admin-password"].arn },
  ]

  # The worker's engine declaration: delegated coding work runs as one-shot Fargate
  # tasks in THIS cluster, isolated by the envs security group (git smart-HTTP to the
  # api and the internet; no database, no EFS).
  exec_engines = jsonencode([{
    key                 = "aws"
    kind                = "aws-ecs"
    label               = "AWS Fargate"
    region              = var.region
    cluster             = aws_ecs_cluster.this.name
    subnets             = aws_subnet.public[*].id
    security_groups     = [aws_security_group.envs.id]
    execution_role_arn  = aws_iam_role.execution.arn
    log_group           = aws_cloudwatch_log_group.exec_envs.name
    assign_public_ip    = true
    run_timeout_seconds = 1800
  }])

  log_config = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.app.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = "svc"
    }
  }

  efs_volumes = {
    blobs = { access_point = aws_efs_access_point.blobs.id, path = "/app/data/blobs" }
    hf    = { access_point = aws_efs_access_point.hf.id, path = "/app/.cache/huggingface" }
  }
}

# --- task definitions ------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.app_task.arn

  dynamic "volume" {
    for_each = local.efs_volumes
    content {
      name = volume.key
      efs_volume_configuration {
        file_system_id     = aws_efs_file_system.this.id
        transit_encryption = "ENABLED"
        authorization_config {
          access_point_id = volume.value.access_point
        }
      }
    }
  }

  container_definitions = jsonencode([{
    name             = "api"
    image            = local.app_image
    command          = ["api"]
    essential        = true
    portMappings     = [{ containerPort = 8000, protocol = "tcp" }]
    environment      = local.app_environment
    secrets          = local.app_secrets
    logConfiguration = local.log_config
    mountPoints = [
      for name, v in local.efs_volumes : { sourceVolume = name, containerPath = v.path }
    ]
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  dynamic "volume" {
    for_each = local.efs_volumes
    content {
      name = volume.key
      efs_volume_configuration {
        file_system_id     = aws_efs_file_system.this.id
        transit_encryption = "ENABLED"
        authorization_config {
          access_point_id = volume.value.access_point
        }
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.app_image
    command   = ["worker"]
    essential = true
    environment = concat(local.app_environment, [
      { name = "PYRRHULA_EXEC_ENGINES", value = local.exec_engines },
    ])
    secrets          = local.app_secrets
    logConfiguration = local.log_config
    mountPoints = [
      for name, v in local.efs_volumes : { sourceVolume = name, containerPath = v.path }
    ]
  }])
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${var.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.app_task.arn

  container_definitions = jsonencode([{
    name         = "web"
    image        = local.web_image
    essential    = true
    portMappings = [{ containerPort = 80, protocol = "tcp" }]
    environment = [
      { name = "PYRRHULA_API_UPSTREAM", value = "${local.api_host}:8000" },
    ]
    logConfiguration = local.log_config
  }])
}

resource "aws_ecs_task_definition" "admin" {
  family                   = "${var.name}-admin"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.app_task.arn

  container_definitions = jsonencode([{
    name             = "admin"
    image            = local.app_image
    command          = ["admin"]
    essential        = true
    portMappings     = [{ containerPort = 8100, protocol = "tcp" }]
    environment      = local.app_environment
    secrets          = local.app_secrets
    logConfiguration = local.log_config
  }])
}

# One-off: run with deploy/aws/migrate.sh after every image push that changes the
# schema (and once on first install, before anything else works).
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.app_task.arn

  container_definitions = jsonencode([{
    name             = "migrate"
    image            = local.app_image
    command          = ["migrate"]
    essential        = true
    environment      = local.app_environment
    secrets          = local.app_secrets
    logConfiguration = local.log_config
  }])
}

# --- services --------------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name                   = "api"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.api.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  service_registries {
    registry_arn = aws_service_discovery_service.api.arn
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener_rule.api]
}

resource "aws_ecs_service" "worker" {
  name                   = "worker"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.worker.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }
}

# Agent web search: same bundled SearXNG as the compose stack (image built from
# docker/searxng.Dockerfile -- the JSON API the platform queries needs a settings
# file, which Fargate cannot bind-mount).
resource "aws_service_discovery_service" "searxng" {
  name = "searxng"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id
    dns_records {
      type = "A"
      ttl  = 10
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_ecs_task_definition" "searxng" {
  family                   = "${var.name}-searxng"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.app_task.arn

  container_definitions = jsonencode([{
    name         = "searxng"
    image        = "${aws_ecr_repository.searxng.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    secrets = [
      { name = "SEARXNG_SECRET", valueFrom = aws_secretsmanager_secret.this["jwt-secret"].arn },
    ]
    logConfiguration = local.log_config
  }])
}

resource "aws_ecs_service" "searxng" {
  name            = "searxng"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.searxng.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  service_registries {
    registry_arn = aws_service_discovery_service.searxng.arn
  }
}

resource "aws_ecs_service" "web" {
  name            = "web"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 80
  }

  depends_on = [aws_lb_listener.web]
}

resource "aws_ecs_service" "admin" {
  count           = length(var.admin_cidrs) > 0 ? 1 : 0
  name            = "admin"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.admin.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.admin[0].arn
    container_name   = "admin"
    container_port   = 8100
  }

  depends_on = [aws_lb_listener.admin]
}
