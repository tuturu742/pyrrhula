# Demo-grade networking: one VPC, two PUBLIC subnets (two AZs -- ALB and RDS both
# require it), no NAT gateway. App tasks get public IPs for outbound (image pulls,
# model APIs, HuggingFace); nothing inbound reaches them except through the ALB and
# the intra-VPC security-group rules below.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private DNS: web's nginx proxies /api/ to api.<name>.local, exec-env tasks clone
# over git smart-HTTP from the same name -- no traffic through the public ALB.
resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.name}.local"
  vpc  = aws_vpc.this.id
}

resource "aws_service_discovery_service" "api" {
  name = "api"
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

# --- security groups -------------------------------------------------------------

resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "web UI"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  dynamic "ingress" {
    for_each = length(var.admin_cidrs) > 0 ? [1] : []
    content {
      description = "platform-admin console"
      from_port   = 8100
      to_port     = 8100
      protocol    = "tcp"
      cidr_blocks = var.admin_cidrs
    }
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# api / worker / web / admin tasks. Ingress: ALB + each other (web->api proxy,
# exec envs -> api git smart-HTTP comes from the envs SG below).
resource "aws_security_group" "app" {
  name_prefix = "${var.name}-app-"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = 0
    to_port         = 65535
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port = 0
    to_port   = 65535
    protocol  = "tcp"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# One-shot exec environments (delegated coding agents). They reach the api's git
# smart-HTTP endpoint and the internet (package registries) -- and NOTHING else in
# the stack: no database, no redis, no EFS.
resource "aws_security_group" "envs" {
  name_prefix = "${var.name}-envs-"
  vpc_id      = aws_vpc.this.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "api_from_envs" {
  type                     = "ingress"
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.app.id
  source_security_group_id = aws_security_group.envs.id
  description              = "exec envs: git smart-HTTP only"
}

# The reverse direction, and the only one previews need: a preview task serves the
# built app on 8080 and the api proxies it (previews are never exposed directly --
# the share link terminates at the api, which stamps the COOP/COEP headers). Without
# this the envs SG has egress only and the proxy hop is unreachable on Fargate.
resource "aws_security_group_rule" "envs_preview_from_app" {
  type                     = "ingress"
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  security_group_id        = aws_security_group.envs.id
  source_security_group_id = aws_security_group.app.id
  description              = "api preview proxy -> preview task"
}

resource "aws_security_group" "db" {
  name_prefix = "${var.name}-db-"
  vpc_id      = aws_vpc.this.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.name}-redis-"
  vpc_id      = aws_vpc.this.id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "efs" {
  name_prefix = "${var.name}-efs-"
  vpc_id      = aws_vpc.this.id
  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
