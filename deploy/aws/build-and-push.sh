#!/usr/bin/env bash
# Build both images from the repo root and push them to the ECR repos Terraform
# created. Works with docker or podman (whichever is on PATH; docker wins).
set -euo pipefail
cd "$(dirname "$0")"

ENGINE=$(command -v docker || command -v podman)
TF=$(command -v terraform || command -v tofu)
REGION=$("$TF" output -raw region)
ECR_APP=$("$TF" output -raw ecr_app)
ECR_WEB=$("$TF" output -raw ecr_web)
ECR_SEARXNG=$("$TF" output -raw ecr_searxng)
TAG="${IMAGE_TAG:-latest}"
REGISTRY="${ECR_APP%%/*}"

aws ecr get-login-password --region "$REGION" \
  | "$ENGINE" login --username AWS --password-stdin "$REGISTRY"

cd ../..  # repo root
python3 scripts/fetch_plugins.py
# Fargate is linux/amd64; --platform matters when building on ARM (Apple Silicon).
"$ENGINE" build --platform linux/amd64 -t "$ECR_APP:$TAG" -f docker/Dockerfile .
"$ENGINE" build --platform linux/amd64 -t "$ECR_WEB:$TAG" -f docker/web.Dockerfile .
"$ENGINE" build --platform linux/amd64 -t "$ECR_SEARXNG:$TAG" -f docker/searxng.Dockerfile .
"$ENGINE" push "$ECR_APP:$TAG"
"$ENGINE" push "$ECR_WEB:$TAG"
"$ENGINE" push "$ECR_SEARXNG:$TAG"

CLUSTER=$(cd deploy/aws && "$TF" output -raw ecs_cluster)
echo
echo "Pushed. If this is the first push (or a schema change): ./migrate.sh"
echo "Services pull the new image on their next deployment:"
echo "  aws ecs update-service --cluster $CLUSTER --service api --force-new-deployment --region $REGION"
echo "  (same for worker / web / admin / searxng)"
