variable "region" {
  description = "AWS region for the whole demo stack."
  type        = string
  default     = "eu-central-1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "pyrrhula"
}

variable "vpc_cidr" {
  type    = string
  default = "10.60.0.0/16"
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to reach the web UI (ALB port 80). Tighten for a private demo."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the platform-admin console (ALB port 8100). Empty = console unreachable from outside."
  type        = list(string)
  default     = []
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

# bge-m3 embeddings run in-process in the api and worker; 8 GB keeps the model
# comfortable next to the app. Fargate: 1024 cpu allows 2048-8192 memory.
variable "api_cpu" {
  type    = string
  default = "1024"
}
variable "api_memory" {
  type    = string
  default = "8192"
}
variable "worker_cpu" {
  type    = string
  default = "1024"
}
variable "worker_memory" {
  type    = string
  default = "8192"
}

variable "assistant_model" {
  description = "Cold-start default for the workspace assistant (provider/model). Empty by default, like every other deployment path: baking in a model points a fresh install at something it has no credential for. Set it, then attach the key to the 'Assistant model' connection after first login."
  type        = string
  default     = ""
}

variable "admin_email" {
  description = "Platform admin account created on first boot; its password is generated into Secrets Manager as 'admin-password'."
  type        = string
  default     = "admin@pyrrhula.app"
}

variable "single_tenant_ui" {
  description = "true = hide the organization slug field on login (one-org demo). Defaults FALSE here where compose and k8s default true, on purpose: those two are somebody's own box running one organization, while an AWS deployment is the shape people put several on. Set true if this one is single-org."
  type        = bool
  default     = false
}

variable "hf_offline" {
  description = "1 = load the embedding model strictly from the EFS cache (set after the install's pre-warm; an online HF-hub check can stall and wedge the api). 0 for first boot."
  type        = string
  default     = "0"
}

variable "image_tag" {
  description = "Tag pushed by build-and-push.sh for both images."
  type        = string
  default     = "latest"
}
