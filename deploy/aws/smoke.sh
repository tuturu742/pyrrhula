#!/usr/bin/env bash
# Post-install check on Fargate: the same deploy-smoke.py every other deployment shape
# runs, as a one-shot task on the api task definition (which mounts the same volumes and
# carries the same secrets, so it proves the real thing rather than a lookalike).
#
# Fargate has no `exec` to pipe a script into, which is why the check is baked into the
# image at /app/deploy-smoke.py instead of being piped from the source tree.
set -euo pipefail
cd "$(dirname "$0")"
TF=$(command -v terraform || command -v tofu)

REGION=$("$TF" output -raw region)
CLUSTER=$("$TF" output -raw ecs_cluster)
SUBNETS=$("$TF" output -json subnets | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin)))')
SG=$("$TF" output -raw app_security_group)

TASK_ARN=$(aws ecs run-task \
  --region "$REGION" --cluster "$CLUSTER" \
  --task-definition "${CLUSTER}-api" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","/app/deploy-smoke.py"]}]}' \
  --started-by pyrrhula-smoke \
  --query 'tasks[0].taskArn' --output text)

echo "verification task: $TASK_ARN"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"
EXIT_CODE=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)

# The check's whole value is its message, and on Fargate that message is in CloudWatch
# rather than on this terminal. Say where, precisely, rather than "check the logs".
TASK_ID=${TASK_ARN##*/}
echo "   output: aws logs tail /ecs/${CLUSTER} --region ${REGION} --log-stream-names api/api/${TASK_ID}"
if [ "$EXIT_CODE" != "0" ]; then
  echo "ERROR: the stack is up but cannot do real work (check exited $EXIT_CODE)." >&2
  exit 1
fi
echo "the deployment works."
