resource "aws_lb" "this" {
  name               = var.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  # Session SSE and the assistant chat's NDJSON hold connections open while a model
  # thinks; the 60s default cuts them mid-stream.
  idle_timeout = 300
}

resource "aws_lb_target_group" "web" {
  name        = "${var.name}-web"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"

  health_check {
    path    = "/"
    matcher = "200"
  }
}

resource "aws_lb_listener" "web" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# The api gets its own target group and the ALB routes /api/* and /git/* to it
# directly (the app tolerates the un-stripped /api prefix). Routing through the web
# container's nginx instead would pin the api task's IP at nginx startup and break
# the UI on every api redeploy.
resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"

  health_check {
    path    = "/health"
    matcher = "200"
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.web.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
  condition {
    path_pattern {
      values = ["/api/*", "/git/*"]
    }
  }
}

resource "aws_lb_target_group" "admin" {
  count       = length(var.admin_cidrs) > 0 ? 1 : 0
  name        = "${var.name}-admin"
  port        = 8100
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"

  health_check {
    path = "/"
    # The console is token-gated; any HTTP answer proves liveness.
    matcher = "200-499"
  }
}

resource "aws_lb_listener" "admin" {
  count             = length(var.admin_cidrs) > 0 ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 8100
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.admin[0].arn
  }
}
