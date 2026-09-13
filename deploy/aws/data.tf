# RDS Postgres 16 (pgvector ships as a supported extension; migrations CREATE it),
# ElastiCache Redis, and EFS for the two shared filesystems: the blob/git store
# (api and worker both read+write it) and the HuggingFace cache (bge-m3 downloads
# once, on first boot, then persists).

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = aws_subnet.public[*].id
}

resource "aws_db_instance" "this" {
  identifier                 = var.name
  engine                     = "postgres"
  engine_version             = "16"
  auto_minor_version_upgrade = true
  instance_class             = var.db_instance_class
  allocated_storage          = 20
  storage_type               = "gp3"
  db_name                    = "pyrrhula"
  username                   = "pyrrhula"
  password                   = random_password.postgres.result
  db_subnet_group_name       = aws_db_subnet_group.this.name
  vpc_security_group_ids     = [aws_security_group.db.id]
  publicly_accessible        = false
  skip_final_snapshot        = true
  apply_immediately          = true
}

resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = aws_subnet.public[*].id
}

resource "aws_elasticache_cluster" "this" {
  cluster_id           = var.name
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = [aws_security_group.redis.id]
  apply_immediately    = true
  parameter_group_name = "default.redis7"
}

resource "aws_efs_file_system" "this" {
  creation_token = var.name
  encrypted      = true
  tags           = { Name = var.name }
}

resource "aws_efs_mount_target" "this" {
  count           = 2
  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# The app image runs as uid 10001; access points force ownership so the git store
# and cache are writable without init containers.
resource "aws_efs_access_point" "blobs" {
  file_system_id = aws_efs_file_system.this.id
  posix_user {
    uid = 10001
    gid = 10001
  }
  root_directory {
    path = "/blobs"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0755"
    }
  }
  tags = { Name = "${var.name}-blobs" }
}

resource "aws_efs_access_point" "hf" {
  file_system_id = aws_efs_file_system.this.id
  posix_user {
    uid = 10001
    gid = 10001
  }
  root_directory {
    path = "/hf"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0755"
    }
  }
  tags = { Name = "${var.name}-hf" }
}

resource "aws_ecr_repository" "app" {
  name         = var.name
  force_delete = true
}

resource "aws_ecr_repository" "web" {
  name         = "${var.name}-web"
  force_delete = true
}

resource "aws_ecr_repository" "searxng" {
  name         = "${var.name}-searxng"
  force_delete = true
}
