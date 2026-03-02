#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-all.sh — One-command deployment for both backend and frontend
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "╔══════════════════════════════════════════════╗"
echo "║          Buglyft — Full Deployment           ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Step 1: Verify AWS profile
echo "── Step 1: Checking AWS profile ──"
if ! aws sts get-caller-identity --profile buglyft &>/dev/null; then
    echo "AWS profile 'buglyft' not found. Running setup..."
    bash "$SCRIPT_DIR/setup-aws-profile.sh"
fi
echo "  AWS profile OK"
echo ""

# Step 2: Deploy backend
echo "── Step 2: Deploying Backend (ECS Fargate) ──"
bash "$SCRIPT_DIR/deploy-backend.sh"
echo ""

# Step 3: Deploy frontend
echo "── Step 3: Deploying Frontend (Amplify) ──"
bash "$SCRIPT_DIR/deploy-frontend.sh"
echo ""

echo "╔══════════════════════════════════════════════╗"
echo "║          Deployment Complete!                ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Post-deployment checklist:"
echo "  1. Get backend public IP (see instructions above)"
echo "  2. Update NEXT_PUBLIC_API_URL in Amplify if needed"
echo "  3. Update SITE_URL in backend .env with your Amplify domain"
echo "  4. Add Amplify domain + /auth/callback to Supabase Redirect URLs"
echo "  5. Add Amplify domain to Supabase CORS allowed origins"
echo ""
