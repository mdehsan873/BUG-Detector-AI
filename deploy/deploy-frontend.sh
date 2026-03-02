#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-frontend.sh — Deploy the Next.js frontend to AWS Amplify Hosting
# ─────────────────────────────────────────────────────────────────────────────
# Creates an Amplify app connected to a GitHub repo, sets env vars, and
# triggers the first build.
#
# Prerequisites:
#   1. Run setup-aws-profile.sh first
#   2. Push your code to a GitHub repo
#   3. Create a GitHub Personal Access Token with repo scope
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="buglyft"
REGION=$(aws configure get region --profile "$PROFILE" 2>/dev/null || echo "ap-south-1")

APP_NAME="buglyft-frontend"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "════════════════════════════════════════════════"
echo "  Buglyft Frontend → AWS Amplify Hosting"
echo "════════════════════════════════════════════════"
echo ""

# ── Gather info ───────────────────────────────────────────────────────────────

read -rp "GitHub repo URL (e.g. https://github.com/you/BUG-Detector-AI): " REPO_URL
read -rsp "GitHub Personal Access Token (for Amplify to pull code): " GITHUB_TOKEN
echo ""
read -rp "Git branch to deploy [main]: " BRANCH
BRANCH="${BRANCH:-main}"
read -rp "Backend API URL (e.g. https://api.buglyft.com or http://<IP>:8000): " API_URL

echo ""

# ── 1. Create Amplify app ────────────────────────────────────────────────────

# Check if app already exists
EXISTING_APP_ID=$(aws amplify list-apps --profile "$PROFILE" --region "$REGION" \
    --query "apps[?name=='$APP_NAME'].appId" --output text 2>/dev/null)

if [ -n "$EXISTING_APP_ID" ] && [ "$EXISTING_APP_ID" != "None" ]; then
    echo "[1/4] Amplify app exists: $EXISTING_APP_ID"
    APP_ID="$EXISTING_APP_ID"
else
    echo "[1/4] Creating Amplify app: $APP_NAME"

    # Build spec for Next.js
    BUILD_SPEC=$(cat <<'BUILDEOF'
version: 1
applications:
  - frontend:
      phases:
        preBuild:
          commands:
            - npm ci
        build:
          commands:
            - npm run build
      artifacts:
        baseDirectory: .next
        files:
          - '**/*'
      cache:
        paths:
          - node_modules/**/*
          - .next/cache/**/*
    appRoot: frontend
BUILDEOF
)

    APP_ID=$(aws amplify create-app \
        --name "$APP_NAME" \
        --repository "$REPO_URL" \
        --access-token "$GITHUB_TOKEN" \
        --build-spec "$BUILD_SPEC" \
        --platform WEB_COMPUTE \
        --profile "$PROFILE" --region "$REGION" \
        --query "app.appId" --output text)

    echo "  App created: $APP_ID"
fi

# ── 2. Set environment variables ─────────────────────────────────────────────

echo "[2/4] Setting environment variables..."

aws amplify update-app \
    --app-id "$APP_ID" \
    --environment-variables "NEXT_PUBLIC_API_URL=$API_URL" \
    --profile "$PROFILE" --region "$REGION" > /dev/null

echo "  NEXT_PUBLIC_API_URL=$API_URL"

# ── 3. Create branch ─────────────────────────────────────────────────────────

EXISTING_BRANCH=$(aws amplify list-branches --app-id "$APP_ID" \
    --profile "$PROFILE" --region "$REGION" \
    --query "branches[?branchName=='$BRANCH'].branchName" --output text 2>/dev/null)

if [ -n "$EXISTING_BRANCH" ] && [ "$EXISTING_BRANCH" != "None" ]; then
    echo "[3/4] Branch '$BRANCH' already connected"
else
    echo "[3/4] Connecting branch: $BRANCH"
    aws amplify create-branch \
        --app-id "$APP_ID" \
        --branch-name "$BRANCH" \
        --framework "Next.js - SSR" \
        --stage PRODUCTION \
        --profile "$PROFILE" --region "$REGION" > /dev/null
fi

# ── 4. Trigger build ─────────────────────────────────────────────────────────

echo "[4/4] Starting deployment..."

JOB_ID=$(aws amplify start-job \
    --app-id "$APP_ID" \
    --branch-name "$BRANCH" \
    --job-type RELEASE \
    --profile "$PROFILE" --region "$REGION" \
    --query "jobSummary.jobId" --output text)

# Get the default domain
DEFAULT_DOMAIN=$(aws amplify get-app --app-id "$APP_ID" \
    --profile "$PROFILE" --region "$REGION" \
    --query "app.defaultDomain" --output text)

echo ""
echo "════════════════════════════════════════════════"
echo "  Frontend deployment started!"
echo "════════════════════════════════════════════════"
echo ""
echo "  App ID:     $APP_ID"
echo "  Branch:     $BRANCH"
echo "  Job ID:     $JOB_ID"
echo ""
echo "  Your site will be live at:"
echo "    https://$BRANCH.$DEFAULT_DOMAIN"
echo ""
echo "  Monitor build progress:"
echo "    aws amplify get-job --app-id $APP_ID --branch-name $BRANCH --job-id $JOB_ID --profile $PROFILE --region $REGION"
echo ""
echo "  IMPORTANT: Update these after deployment:"
echo "    1. Set SITE_URL=$BRANCH.$DEFAULT_DOMAIN in backend .env"
echo "    2. Add https://$BRANCH.$DEFAULT_DOMAIN/auth/callback to"
echo "       Supabase → Authentication → URL Configuration → Redirect URLs"
echo ""
