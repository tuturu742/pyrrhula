#!/usr/bin/env bash
# AWS installer: orchestrates the Terraform stack in deploy/aws (ECS Fargate, RDS,
# ElastiCache, EFS, ALB, Secrets Manager) plus image push and migration.
#
#   deploy/installers/aws.sh [--check] [--auto]
#
# --check  verify prerequisites only.
# --auto   pass -auto-approve to terraform (CI use; interactive apply is the default).
#
# Costs real money (~$115/mo at defaults) -- see deploy/aws/README.md before applying,
# and `terraform destroy` in deploy/aws to tear everything down.
set -euo pipefail
cd "$(dirname "$0")/../.."

say()  { printf '\033[1m== %s\033[0m\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

AUTO=""
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --auto) AUTO="-auto-approve" ;;
    *) fail "unknown flag $arg" ;;
  esac
done

# --- prerequisites ----------------------------------------------------------------
TF=$(command -v terraform || command -v tofu) || fail "need terraform or opentofu"
command -v aws >/dev/null || fail "need the AWS CLI v2"
command -v docker >/dev/null || command -v podman >/dev/null || fail "need docker or podman to build images"
aws sts get-caller-identity >/dev/null 2>&1 || fail "AWS credentials not configured (aws configure / SSO)"
say "AWS account: $(aws sts get-caller-identity --query Account --output text) | tool: $(basename "$TF")"

if [ "$CHECK_ONLY" = 1 ]; then say "prerequisites OK"; exit 0; fi

# --- provision --------------------------------------------------------------------
cd deploy/aws
say "terraform init"
"$TF" init -upgrade >/dev/null
say "terraform apply (RDS is the slow part, ~10-15 min)"
"$TF" apply $AUTO

say "building + pushing images to ECR"
./build-and-push.sh

say "running schema migration"
./migrate.sh

say "pre-warming the embedding model (one-time, ~2.2GB into EFS)"
if ./prewarm.sh; then
  say "switching pods to offline model loading"
  "$TF" apply $AUTO -var hf_offline=1
  REGION=$("$TF" output -raw region); CLUSTER=$("$TF" output -raw ecs_cluster)
  for svc in api worker; do
    aws ecs update-service --region "$REGION" --cluster "$CLUSTER" \
      --service "$svc" --force-new-deployment >/dev/null
  done
fi

say "done."
echo
echo "  Open   $("$TF" output -raw url)  and Sign up (services pull the fresh images"
echo "         within a minute of the push; retry if the first load 503s)."
echo "  Admin  $("$TF" output -raw admin_url 2>/dev/null || echo '(set admin_cidrs to enable)')"
echo "  Keys   all secrets live in AWS Secrets Manager under '$("$TF" output -raw ecs_cluster)/'"
echo "         -- the encryption key there seals every stored credential; never delete it."
echo "  Docs   deploy/aws/README.md (operating, upgrades, costs, teardown)"
