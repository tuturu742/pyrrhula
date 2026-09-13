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
  description = "Cold-start default for the workspace assistant (provider/model). Attach the provider API key to the 'Assistant model' connection in the UI after first login."
  type        = string
  default     = "anthropic/claude-sonnet-5"
}

variable "single_tenant_ui" {
  description = "true = hide the organization slug field on login (one-org demo)."
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
