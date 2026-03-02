#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# redeploy-backend.sh — Quick redeploy: rebuild image + force new deployment
# ─────────────────────────────────────────────────────────────────────────────
# Use this after code changes. It rebuilds the Docker image, pushes to ECR,
# and triggers a rolling update on ECS (zero-downtime).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="buglyft"
REGION=$(aws configure get region --profile "$PROFILE" 2>/dev/null || echo "ap-south-1")
ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

ECR_REPO="buglyft-backend"
ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO"
CLUSTER_NAME="buglyft-cluster"
SERVICE_NAME="buglyft-backend-service"
IMAGE_TAG="latest"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Redeploying backend..."

# Login to ECR
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
    | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

# Build & push
docker build -t "$ECR_REPO:$IMAGE_TAG" "$PROJECT_ROOT/backend"
docker tag "$ECR_REPO:$IMAGE_TAG" "$ECR_URI:$IMAGE_TAG"
docker push "$ECR_URI:$IMAGE_TAG"

# Force new deployment (pulls latest image)
aws ecs update-service \
    --cluster "$CLUSTER_NAME" \
    --service "$SERVICE_NAME" \
    --force-new-deployment \
    --profile "$PROFILE" --region "$REGION" > /dev/null

echo "Done! ECS is rolling out the new image. Takes ~2-3 minutes."
echo "Monitor: aws ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --profile $PROFILE --region $REGION --query 'services[0].deployments'"
