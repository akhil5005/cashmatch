#!/usr/bin/env bash
# Build the two images, push them to ECR, and roll the instance.
#
# The whole deploy is a pull: images are built here (or in CI) and the
# t3.micro only ever pulls, because 1 GB of RAM cannot build a frontend --
# and because deploying the exact bytes that were tested is worth more than
# the convenience of building on the target.
set -euo pipefail

cd "$(dirname "$0")"
REGION="$(terraform -chdir=infra output -raw region)"
API_REPO="$(terraform -chdir=infra output -raw ecr_api)"
UI_REPO="$(terraform -chdir=infra output -raw ecr_ui)"
REGISTRY="${API_REPO%%/*}"
TAG="${1:-latest}"

echo "==> authenticating to $REGISTRY"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> building api"
docker build -q -f backend/Dockerfile.prod -t "$API_REPO:$TAG" backend

echo "==> building ui"
docker build -q -t "$UI_REPO:$TAG" frontend

echo "==> pushing"
docker push -q "$API_REPO:$TAG"
docker push -q "$UI_REPO:$TAG"

if terraform -chdir=infra output -raw instance_id >/dev/null 2>&1; then
  INSTANCE="$(terraform -chdir=infra output -raw instance_id)"
  echo "==> rolling $INSTANCE"
  aws ssm send-command --region "$REGION" \
    --instance-ids "$INSTANCE" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["systemctl start cashmatch-refresh"]' \
    --query 'Command.CommandId' --output text
  echo "==> $(terraform -chdir=infra output -raw url)"
fi
