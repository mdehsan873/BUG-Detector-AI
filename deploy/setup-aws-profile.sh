#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-aws-profile.sh — Configure a named AWS CLI profile for Buglyft
# ─────────────────────────────────────────────────────────────────────────────
# Usage:  ./setup-aws-profile.sh
#
# This creates (or updates) an AWS CLI profile named "buglyft".
# All other deploy scripts use this profile so credentials stay in ~/.aws/
# and never end up in code, env files, or chat logs.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROFILE="buglyft"
REGION="${AWS_REGION:-ap-south-1}"   # Change default region if needed

echo "──────────────────────────────────────"
echo " Buglyft — AWS CLI Profile Setup"
echo "──────────────────────────────────────"
echo ""
echo "This will configure the AWS CLI profile: $PROFILE"
echo "Region: $REGION"
echo ""

# Check if aws cli is installed
if ! command -v aws &> /dev/null; then
    echo "ERROR: AWS CLI is not installed."
    echo "Install it from: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    exit 1
fi

# Configure the profile interactively
aws configure --profile "$PROFILE" set region "$REGION"
aws configure --profile "$PROFILE" set output json

echo ""
echo "Enter your AWS credentials (these are stored only in ~/.aws/credentials):"
echo ""
read -rp "  AWS Access Key ID: " access_key
read -rsp "  AWS Secret Access Key: " secret_key
echo ""

aws configure --profile "$PROFILE" set aws_access_key_id "$access_key"
aws configure --profile "$PROFILE" set aws_secret_access_key "$secret_key"

echo ""
echo "Verifying credentials..."
if aws sts get-caller-identity --profile "$PROFILE" &> /dev/null; then
    ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
    echo "  Connected to AWS account: $ACCOUNT_ID"
    echo ""
    echo "Profile '$PROFILE' is ready. All deploy scripts will use this profile."
else
    echo "  ERROR: Could not authenticate. Check your access key and secret."
    exit 1
fi
