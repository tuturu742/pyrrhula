#!/usr/bin/env bash
# Download the embedding model (bge-m3, ~2.2GB) into the shared EFS cache.
#
# NOT an install step any more: which model a deployment runs is chosen in Admin ->
# Models, which downloads into this same cache as a background job. This stays for the
# operator who would rather have the cache populated before anyone logs in -- an
# air-gapped rollout, or simply not wanting the first admin to wait on a download.
# Reuses the api task definition with a command override.
set -euo pipefail
cd "$(dirname "$0")"
TF=$(command -v terraform || command -v tofu)

REGION=$("$TF" output -raw region)
CLUSTER=$("$TF" output -raw ecs_cluster)
FAMILY=$("$TF" output -raw migrate_task_family)
SUBNETS=$("$TF" output -json subnets | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin)))')
SG=$("$TF" output -raw app_security_group)

# The api task definition mounts the EFS cache -- run it one-shot with a command
# override (the migrate family has no volumes).
TASK_ARN=$(aws ecs run-task \
  --region "$REGION" --cluster "$CLUSTER" \
  --task-definition "${CLUSTER}-api" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","-c","from sentence_transformers import SentenceTransformer; SentenceTransformer(\"BAAI/bge-m3\")"],"environment":[{"name":"HF_HUB_OFFLINE","value":"0"},{"name":"TRANSFORMERS_OFFLINE","value":"0"}]}]}' \
  --started-by pyrrhula-prewarm \
  --query 'tasks[0].taskArn' --output text)

echo "pre-warm task: $TASK_ARN (downloading ~2.2GB -- takes a few minutes)"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"
EXIT_CODE=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
if [ "$EXIT_CODE" != "0" ]; then
  echo "pre-warm FAILED (exit $EXIT_CODE) -- the first knowledge call will download instead" >&2
  exit 1
fi
echo "pre-warm OK"
