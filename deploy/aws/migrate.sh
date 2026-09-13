#!/usr/bin/env bash
# Run the one-off schema migration task and wait for it. Safe to re-run any time
# (Alembic is idempotent); required on first install and after schema changes.
set -euo pipefail
cd "$(dirname "$0")"

TF=$(command -v terraform || command -v tofu)
REGION=$("$TF" output -raw region)
CLUSTER=$("$TF" output -raw ecs_cluster)
FAMILY=$("$TF" output -raw migrate_task_family)
SUBNETS=$("$TF" output -json subnets | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin)))')
SG=$("$TF" output -raw app_security_group)

TASK_ARN=$(aws ecs run-task \
  --region "$REGION" --cluster "$CLUSTER" \
  --task-definition "$FAMILY" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --started-by pyrrhula-migrate \
  --query 'tasks[0].taskArn' --output text)

echo "migration task: $TASK_ARN"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"

EXIT_CODE=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
TASK_ID="${TASK_ARN##*/}"
echo "--- migration logs ---"
aws logs get-log-events --region "$REGION" \
  --log-group-name "/$("$TF" output -raw ecs_cluster)/app" \
  --log-stream-name "svc/migrate/$TASK_ID" \
  --query 'events[].message' --output text || true
echo "----------------------"
if [ "$EXIT_CODE" != "0" ]; then
  echo "migration FAILED (exit $EXIT_CODE)" >&2
  exit 1
fi
echo "migration OK"
