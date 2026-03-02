#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# redeploy-backend.sh — Quick redeploy: rebuild image + update task def + deploy
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="buglyft"
REGION=$(aws configure get region --profile "$PROFILE" 2>/dev/null || echo "us-east-1")
ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

ECR_REPO="buglyft-backend"
ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO"
CLUSTER_NAME="buglyft-cluster"
SERVICE_NAME="buglyft-backend-service"
TASK_FAMILY="buglyft-backend-task"
CONTAINER_NAME="buglyft-backend"
CONTAINER_PORT=8000
IMAGE_TAG="latest"
LOG_GROUP="/ecs/buglyft-backend"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$SCRIPT_DIR/.env.backend"

echo "════════════════════════════════════════════════"
echo "  Redeploying backend..."
echo "════════════════════════════════════════════════"

# ── 1. Load secrets ──────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found."
    exit 1
fi

source "$ENV_FILE"
echo "[1/4] Secrets loaded"

# ── 2. Build & push image ───────────────────────────────────────────────────
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
    | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

echo "[2/4] Building Docker image (--no-cache)..."
docker build --platform linux/amd64 --no-cache -t "$ECR_REPO:$IMAGE_TAG" "$PROJECT_ROOT/backend"
docker tag "$ECR_REPO:$IMAGE_TAG" "$ECR_URI:$IMAGE_TAG"
docker push "$ECR_URI:$IMAGE_TAG"
echo "  Image pushed: $ECR_URI:$IMAGE_TAG"

# ── 3. Re-register task definition ──────────────────────────────────────────
echo "[3/4] Registering updated task definition..."

EXEC_ROLE_ARN=$(aws iam get-role --role-name "buglyft-ecs-execution-role" --profile "$PROFILE" \
    --query "Role.Arn" --output text)

SITE_URL_VAL="${SITE_URL:-http://localhost:3000}"
CORS_ORIGINS_VAL="${CORS_ORIGINS:-http://localhost:3000,https://main.dmmdetom9xhhv.amplifyapp.com,https://buglyft.com,https://www.buglyft.com}"

TASK_DEF=$(cat <<TASKEOF
{
    "family": "$TASK_FAMILY",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "512",
    "memory": "1024",
    "executionRoleArn": "$EXEC_ROLE_ARN",
    "taskRoleArn": "$EXEC_ROLE_ARN",
    "containerDefinitions": [{
        "name": "$CONTAINER_NAME",
        "image": "$ECR_URI:$IMAGE_TAG",
        "essential": true,
        "portMappings": [{
            "containerPort": $CONTAINER_PORT,
            "protocol": "tcp"
        }],
        "environment": [
            {"name": "SUPABASE_URL",    "value": "$SUPABASE_URL"},
            {"name": "SUPABASE_KEY",    "value": "$SUPABASE_KEY"},
            {"name": "ENCRYPTION_KEY",  "value": "$ENCRYPTION_KEY"},
            {"name": "OPENAI_API_KEY",  "value": "$OPENAI_API_KEY"},
            {"name": "SITE_URL",        "value": "$SITE_URL_VAL"},
            {"name": "CORS_ORIGINS",    "value": "$CORS_ORIGINS_VAL"}
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "$LOG_GROUP",
                "awslogs-region": "$REGION",
                "awslogs-stream-prefix": "ecs"
            }
        },
        "healthCheck": {
            "command": ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/docs')\" || exit 1"],
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 60
        }
    }]
}
TASKEOF
)

echo "$TASK_DEF" > "/tmp/buglyft-task-def.json"
aws ecs register-task-definition \
    --cli-input-json file:///tmp/buglyft-task-def.json \
    --profile "$PROFILE" --region "$REGION" > /dev/null
rm -f /tmp/buglyft-task-def.json
echo "  Task definition updated"

# ── 4. Force new deployment ──────────────────────────────────────────────────
echo "[4/4] Forcing new deployment..."
aws ecs update-service \
    --cluster "$CLUSTER_NAME" \
    --service "$SERVICE_NAME" \
    --task-definition "$TASK_FAMILY" \
    --force-new-deployment \
    --profile "$PROFILE" --region "$REGION" > /dev/null

echo ""
echo "════════════════════════════════════════════════"
echo "  Redeployment triggered!"
echo "════════════════════════════════════════════════"
echo "  ECS will roll out the new image in ~2-3 minutes."
echo ""
echo "  Check status:"
echo "    aws ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --profile $PROFILE --region $REGION --query 'services[0].{running:runningCount,desired:desiredCount}'"
echo ""
echo "  Check logs:"
echo "    aws logs tail /ecs/buglyft-backend --profile $PROFILE --region $REGION --since 5m --format short"
echo ""
