data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what ECS itself needs to START a container -- ECR pull, awslogs,
# and reading the Secrets Manager entries referenced by task definitions. Shared by
# the app services AND the one-shot exec-env tasks (those reference no secrets).
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-app-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.this : s.arn]
    }]
  })
}

# Task role for the WORKER: the aws-ecs exec engine registers task definitions and
# runs/stops one-shot Fargate tasks (see docs/exec-engines.md for this exact list).
resource "aws_iam_role" "worker_task" {
  name               = "${var.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "worker_exec_engine" {
  name = "exec-engine"
  role = aws_iam_role.worker_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:RunTask",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:StopTask",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.execution.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents"]
        Resource = "${aws_cloudwatch_log_group.exec_envs.arn}:*"
      },
    ]
  })
}

# api/admin/web/migrate need no AWS API access of their own -- except the SSM
# channel ECS Exec uses (`aws ecs execute-command`), the only shell into Fargate.
resource "aws_iam_role" "app_task" {
  name               = "${var.name}-app-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "ecs_exec_channel" {
  statement {
    effect = "Allow"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "app_task_exec_channel" {
  name   = "ecs-exec"
  role   = aws_iam_role.app_task.id
  policy = data.aws_iam_policy_document.ecs_exec_channel.json
}

resource "aws_iam_role_policy" "worker_task_exec_channel" {
  name   = "ecs-exec"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.ecs_exec_channel.json
}
