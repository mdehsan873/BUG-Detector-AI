#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-backend.sh — Deploy the FastAPI backend to AWS ECS Fargate
# ─────────────────────────────────────────────────────────────────────────────
# Creates:  ECR repo → pushes Docker image → ECS cluster + service + ALB
#
# Prerequisites:
#   1. Run setup-aws-profile.sh first
#   2. Set secrets in deploy/.env.backend (see deploy/.env.backend.example)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="buglyft"
REGION=$(aws configure get region --profile "$PROFILE" 2>/dev/null || echo "us-east-1")
ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

APP_NAME="buglyft-backend"
ECR_REPO="$APP_NAME"
CLUSTER_NAME="buglyft-cluster"
SERVICE_NAME="$APP_NAME-service"
TASK_FAMILY="$APP_NAME-task"
CONTAINER_NAME="$APP_NAME"
CONTAINER_PORT=8000
IMAGE_TAG="latest"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$SCRIPT_DIR/.env.backend"

echo "════════════════════════════════════════════════"
echo "  Buglyft Backend → AWS ECS Fargate"
echo "════════════════════════════════════════════════"
echo "  Account:  $ACCOUNT_ID"
echo "  Region:   $REGION"
echo "  Profile:  $PROFILE"
echo ""

# ── 1. Load & validate secrets ────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found."
    echo "Copy deploy/.env.backend.example → deploy/.env.backend and fill in values."
    exit 1
fi

source "$ENV_FILE"

for var in SUPABASE_URL SUPABASE_KEY ENCRYPTION_KEY OPENAI_API_KEY; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: $var is not set in $ENV_FILE"
        exit 1
    fi
done

echo "[1/8] Secrets loaded from .env.backend"

# ── 2. Create ECR repository ─────────────────────────────────────────────────

ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO"

if ! aws ecr describe-repositories --repository-names "$ECR_REPO" --profile "$PROFILE" --region "$REGION" &>/dev/null; then
    echo "[2/8] Creating ECR repository: $ECR_REPO"
    aws ecr create-repository \
        --repository-name "$ECR_REPO" \
        --image-scanning-configuration scanOnPush=true \
        --profile "$PROFILE" --region "$REGION" > /dev/null
else
    echo "[2/8] ECR repository exists: $ECR_REPO"
fi

# ── 3. Build & push Docker image ─────────────────────────────────────────────

echo "[3/8] Logging into ECR..."
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
    | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

echo "[3/8] Building Docker image..."
docker build --platform linux/amd64 -t "$ECR_REPO:$IMAGE_TAG" "$PROJECT_ROOT/backend"

echo "[3/8] Pushing image to ECR..."
docker tag "$ECR_REPO:$IMAGE_TAG" "$ECR_URI:$IMAGE_TAG"
docker push "$ECR_URI:$IMAGE_TAG"

echo "[3/8] Image pushed: $ECR_URI:$IMAGE_TAG"

# ── 4. Create ECS cluster ────────────────────────────────────────────────────

if ! aws ecs describe-clusters --clusters "$CLUSTER_NAME" --profile "$PROFILE" --region "$REGION" \
    --query "clusters[?status=='ACTIVE'].clusterName" --output text | grep -q "$CLUSTER_NAME"; then
    echo "[4/8] Creating ECS cluster: $CLUSTER_NAME"
    aws ecs create-cluster --cluster-name "$CLUSTER_NAME" \
        --profile "$PROFILE" --region "$REGION" > /dev/null
else
    echo "[4/8] ECS cluster exists: $CLUSTER_NAME"
fi

# ── 5. Create IAM execution role ─────────────────────────────────────────────

EXEC_ROLE_NAME="buglyft-ecs-execution-role"
EXEC_ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/$EXEC_ROLE_NAME"

if ! aws iam get-role --role-name "$EXEC_ROLE_NAME" --profile "$PROFILE" &>/dev/null; then
    echo "[5/8] Creating ECS execution role..."
    aws iam create-role \
        --role-name "$EXEC_ROLE_NAME" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }' \
        --profile "$PROFILE" > /dev/null

    aws iam attach-role-policy \
        --role-name "$EXEC_ROLE_NAME" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" \
        --profile "$PROFILE"

    # Allow pulling from ECR and writing logs
    aws iam attach-role-policy \
        --role-name "$EXEC_ROLE_NAME" \
        --policy-arn "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess" \
        --profile "$PROFILE"

    EXEC_ROLE_ARN=$(aws iam get-role --role-name "$EXEC_ROLE_NAME" --profile "$PROFILE" \
        --query "Role.Arn" --output text)
else
    echo "[5/8] Execution role exists: $EXEC_ROLE_NAME"
    EXEC_ROLE_ARN=$(aws iam get-role --role-name "$EXEC_ROLE_NAME" --profile "$PROFILE" \
        --query "Role.Arn" --output text)
fi

# ── 6. Create CloudWatch log group ───────────────────────────────────────────

LOG_GROUP="/ecs/$APP_NAME"
if ! aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --profile "$PROFILE" --region "$REGION" \
    --query "logGroups[?logGroupName=='$LOG_GROUP'].logGroupName" --output text | grep -q "$LOG_GROUP"; then
    echo "[6/8] Creating CloudWatch log group: $LOG_GROUP"
    aws logs create-log-group --log-group-name "$LOG_GROUP" \
        --profile "$PROFILE" --region "$REGION"
else
    echo "[6/8] Log group exists: $LOG_GROUP"
fi

# ── 7. Register task definition ──────────────────────────────────────────────

echo "[7/8] Registering task definition..."

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
            "command": ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:$CONTAINER_PORT/docs')\" || exit 1"],
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

echo "  Task definition registered: $TASK_FAMILY"

# ── 8. Create or update the ECS service ──────────────────────────────────────

# Get default VPC and subnets
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --profile "$PROFILE" --region "$REGION" \
    --query "Vpcs[0].VpcId" --output text)

SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
    --profile "$PROFILE" --region "$REGION" \
    --query "Subnets[*].SubnetId" --output text | tr '\t' ',')

# Create security group for the backend
SG_NAME="buglyft-backend-sg"
SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --profile "$PROFILE" --region "$REGION" \
    --query "SecurityGroups[0].GroupId" --output text 2>/dev/null)

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    echo "[8/8] Creating security group..."
    SG_ID=$(aws ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "Buglyft backend - allow HTTP 8000" \
        --vpc-id "$VPC_ID" \
        --profile "$PROFILE" --region "$REGION" \
        --query "GroupId" --output text)

    # Allow inbound on port 8000 from anywhere (ALB will front this)
    aws ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --protocol tcp --port 8000 --cidr 0.0.0.0/0 \
        --profile "$PROFILE" --region "$REGION" > /dev/null
fi

# Check if service exists
EXISTING_SERVICE=$(aws ecs describe-services \
    --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME" \
    --profile "$PROFILE" --region "$REGION" \
    --query "services[?status=='ACTIVE'].serviceName" --output text 2>/dev/null)

if [ -n "$EXISTING_SERVICE" ] && [ "$EXISTING_SERVICE" != "None" ]; then
    echo "[8/8] Updating ECS service (new image)..."
    aws ecs update-service \
        --cluster "$CLUSTER_NAME" \
        --service "$SERVICE_NAME" \
        --task-definition "$TASK_FAMILY" \
        --force-new-deployment \
        --profile "$PROFILE" --region "$REGION" > /dev/null
else
    echo "[8/8] Creating ECS Fargate service..."
    aws ecs create-service \
        --cluster "$CLUSTER_NAME" \
        --service-name "$SERVICE_NAME" \
        --task-definition "$TASK_FAMILY" \
        --desired-count 1 \
        --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
        --profile "$PROFILE" --region "$REGION" > /dev/null
fi

echo ""
echo "════════════════════════════════════════════════"
echo "  Backend deployed successfully!"
echo "════════════════════════════════════════════════"
echo ""
echo "  Cluster:    $CLUSTER_NAME"
echo "  Service:    $SERVICE_NAME"
echo "  Image:      $ECR_URI:$IMAGE_TAG"
echo ""
echo "  To get the public IP of the running task:"
echo "    TASK_ARN=\$(aws ecs list-tasks --cluster $CLUSTER_NAME --service-name $SERVICE_NAME --profile $PROFILE --region $REGION --query 'taskArns[0]' --output text)"
echo "    ENI_ID=\$(aws ecs describe-tasks --cluster $CLUSTER_NAME --tasks \$TASK_ARN --profile $PROFILE --region $REGION --query 'tasks[0].attachments[0].details[?name==\`networkInterfaceId\`].value' --output text)"
echo "    aws ec2 describe-network-interfaces --network-interface-ids \$ENI_ID --profile $PROFILE --region $REGION --query 'NetworkInterfaces[0].Association.PublicIp' --output text"
echo ""
echo "  Next: Set NEXT_PUBLIC_API_URL to http://<PUBLIC_IP>:8000 for the frontend."
echo ""
