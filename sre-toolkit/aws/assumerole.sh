#!/usr/bin/env bash

###############################################################################
# GitHub Actions OIDC -> AWS Administrator Role
#
# PURPOSE:
#   Create an AWS IAM role that ALL repositories in a GitHub organization
#   can assume through GitHub Actions OIDC.
#
# ACCESS:
#   AdministratorAccess
#
# TRUST:
#   repo:GITHUB_ORG/*
#
# EXAMPLE:
#   repo:mycompany/*
#
# WARNING:
#   This gives EVERY repository in the specified GitHub organization
#   administrator access to the AWS account.
###############################################################################

set -euo pipefail

###############################################################################
# CONFIGURATION
###############################################################################

AWS_REGION="${AWS_REGION:-us-east-1}"

ROLE_NAME="${ROLE_NAME:-ecs-platform-github-admin}"

OIDC_URL="https://token.actions.githubusercontent.com"

ADMIN_POLICY_ARN="arn:aws:iam::aws:policy/AdministratorAccess"

###############################################################################
# HEADER
###############################################################################

echo
echo "============================================================"
echo " GitHub OIDC -> AWS Administrator Role"
echo " ALL REPOSITORIES IN GITHUB ORGANIZATION"
echo "============================================================"
echo

###############################################################################
# CHECK AWS CLI
###############################################################################

if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: AWS CLI is not installed."
    exit 1
fi

###############################################################################
# GET CURRENT AWS ACCOUNT
###############################################################################

ACCOUNT_ID=$(aws sts get-caller-identity \
    --query 'Account' \
    --output text)

CALLER_ARN=$(aws sts get-caller-identity \
    --query 'Arn' \
    --output text)

echo "AWS Account : ${ACCOUNT_ID}"
echo "Caller      : ${CALLER_ARN}"
echo "AWS Region  : ${AWS_REGION}"
echo "Role Name   : ${ROLE_NAME}"
echo

###############################################################################
# GET GITHUB ORGANIZATION
###############################################################################

read -rp "GitHub Organization: " GITHUB_ORG

if [[ -z "${GITHUB_ORG}" ]]; then
    echo "ERROR: GitHub organization cannot be empty."
    exit 1
fi

###############################################################################
# OIDC PROVIDER ARN
###############################################################################

OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

###############################################################################
# DISPLAY CONFIGURATION
###############################################################################

echo
echo "============================================================"
echo " Configuration"
echo "============================================================"

echo "GitHub Organization : ${GITHUB_ORG}"
echo "GitHub Trust        : repo:${GITHUB_ORG}/*"
echo "AWS Role            : ${ROLE_NAME}"
echo "AWS Permissions     : AdministratorAccess"
echo

###############################################################################
# CONFIRM
###############################################################################

echo "WARNING:"
echo "Every GitHub repository in ${GITHUB_ORG} will be able to"
echo "attempt to assume this AWS Administrator role."
echo

read -rp "Continue? [y/N]: " CONFIRM

if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

###############################################################################
# CREATE GITHUB OIDC PROVIDER
###############################################################################

echo
echo "============================================================"
echo " Checking GitHub OIDC Provider"
echo "============================================================"

if aws iam get-open-id-connect-provider \
    --open-id-connect-provider-arn "${OIDC_ARN}" \
    >/dev/null 2>&1; then

    echo "GitHub OIDC provider already exists."

else

    echo "Creating GitHub OIDC provider..."

    aws iam create-open-id-connect-provider \
        --url "${OIDC_URL}" \
        --client-id-list "sts.amazonaws.com"

    echo "GitHub OIDC provider created."

fi

###############################################################################
# CREATE TRUST POLICY
###############################################################################

TRUST_POLICY="/tmp/${ROLE_NAME}-trust-policy.json"

cat > "${TRUST_POLICY}" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubActionsOIDCAllRepositories",
      "Effect": "Allow",
      "Principal": {
        "Federated": "${OIDC_ARN}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:${GITHUB_ORG}/*"
        }
      }
    }
  ]
}
EOF

###############################################################################
# DISPLAY TRUST POLICY
###############################################################################

echo
echo "============================================================"
echo "Trust Policy"
echo "============================================================"

cat "${TRUST_POLICY}"

###############################################################################
# CREATE OR UPDATE ROLE
###############################################################################

echo
echo "============================================================"
echo "Checking IAM Role"
echo "============================================================"

if aws iam get-role \
    --role-name "${ROLE_NAME}" \
    >/dev/null 2>&1; then

    echo "Role already exists:"
    echo "${ROLE_NAME}"

    echo
    echo "Updating trust policy..."

    aws iam update-assume-role-policy \
        --role-name "${ROLE_NAME}" \
        --policy-document "file://${TRUST_POLICY}"

    echo "Trust policy updated."

else

    echo "Creating role..."

    aws iam create-role \
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document "file://${TRUST_POLICY}" \
        --description \
        "GitHub Actions OIDC administrator role for all repositories"

    echo "Role created."

fi

###############################################################################
# ATTACH ADMINISTRATOR ACCESS
###############################################################################

echo
echo "============================================================"
echo "Attaching AdministratorAccess"
echo "============================================================"

aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn "${ADMIN_POLICY_ARN}"

echo "AdministratorAccess attached."

###############################################################################
# GET ROLE ARN
###############################################################################

ROLE_ARN=$(aws iam get-role \
    --role-name "${ROLE_NAME}" \
    --query 'Role.Arn' \
    --output text)

###############################################################################
# VERIFY ROLE
###############################################################################

echo
echo "============================================================"
echo "ROLE"
echo "============================================================"

echo
echo "Role Name:"
echo "${ROLE_NAME}"

echo
echo "Role ARN:"
echo "${ROLE_ARN}"

###############################################################################
# VERIFY POLICIES
###############################################################################

echo
echo "============================================================"
echo "ATTACHED POLICIES"
echo "============================================================"

aws iam list-attached-role-policies \
    --role-name "${ROLE_NAME}" \
    --query 'AttachedPolicies[*].[PolicyName,PolicyArn]' \
    --output table

###############################################################################
# VERIFY OIDC
###############################################################################

echo
echo "============================================================"
echo "OIDC PROVIDER"
echo "============================================================"

aws iam get-open-id-connect-provider \
    --open-id-connect-provider-arn "${OIDC_ARN}" \
    --query '{URL:Url,ClientIDs:ClientIDList}' \
    --output json

###############################################################################
# GITHUB ACTIONS WORKFLOW
###############################################################################

echo
echo "============================================================"
echo " GITHUB ACTIONS"
echo "============================================================"

cat <<EOF

Use this in ANY repository in:

    ${GITHUB_ORG}

GitHub Actions workflow:

permissions:
  id-token: write
  contents: read

jobs:

  terraform:
    runs-on: ubuntu-latest

    steps:

      - name: Checkout
        uses: actions/checkout@v4

      - name: Configure AWS Credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${ROLE_ARN}
          aws-region: ${AWS_REGION}

      - name: Verify AWS Identity
        run: aws sts get-caller-identity

EOF

###############################################################################
# TERRAFORM OUTPUT
###############################################################################

echo
echo "============================================================"
echo " TERRAFORM / AWS ROLE ARN"
echo "============================================================"

echo "${ROLE_ARN}"

###############################################################################
# TRUST RELATIONSHIP
###############################################################################

echo
echo "============================================================"
echo " TRUST RELATIONSHIP"
echo "============================================================"

echo "GitHub Organization:"
echo "${GITHUB_ORG}"

echo
echo "Allowed repositories:"
echo "repo:${GITHUB_ORG}/*"

echo
echo "AWS permissions:"
echo "AdministratorAccess"

###############################################################################
# CLEANUP
###############################################################################

rm -f "${TRUST_POLICY}"

echo
echo "============================================================"
echo " COMPLETE"
echo "============================================================"

echo
echo "All repositories under:"
echo
echo "${GITHUB_ORG}"
echo
echo "can now use GitHub OIDC to assume:"
echo
echo "${ROLE_ARN}"
echo