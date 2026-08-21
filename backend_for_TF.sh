#!/usr/bin/env bash

set -euo pipefail

###############################################################################
# Terraform Remote Backend Bootstrap
#
# S3 Bucket      : bokiti123
# DynamoDB Table : family_dyning
###############################################################################

BUCKET_NAME="bokiti123"
DYNAMODB_TABLE="family_dyning"
AWS_REGION="${AWS_REGION:-us-east-1}"

echo
echo "============================================================"
echo " Terraform Backend Bootstrap"
echo "============================================================"
echo
echo "S3 Bucket      : ${BUCKET_NAME}"
echo "DynamoDB Table : ${DYNAMODB_TABLE}"
echo "AWS Region     : ${AWS_REGION}"
echo

###############################################################################
# Check AWS CLI
###############################################################################

if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: AWS CLI is not installed."
    exit 1
fi

###############################################################################
# Check AWS Credentials
###############################################################################

echo "Checking AWS credentials..."

AWS_ACCOUNT_ID=$(aws sts get-caller-identity \
    --query 'Account' \
    --output text)

AWS_IDENTITY=$(aws sts get-caller-identity \
    --query 'Arn' \
    --output text)

echo "AWS Account : ${AWS_ACCOUNT_ID}"
echo "Identity    : ${AWS_IDENTITY}"
echo

###############################################################################
# Create S3 Bucket
###############################################################################

echo "============================================================"
echo " Creating S3 Bucket"
echo "============================================================"

if aws s3api head-bucket \
    --bucket "${BUCKET_NAME}" \
    --region "${AWS_REGION}" >/dev/null 2>&1; then

    echo "S3 bucket already exists: ${BUCKET_NAME}"

else

    echo "Creating S3 bucket: ${BUCKET_NAME}"

    if [[ "${AWS_REGION}" == "us-east-1" ]]; then

        aws s3api create-bucket \
            --bucket "${BUCKET_NAME}" \
            --region "${AWS_REGION}"

    else

        aws s3api create-bucket \
            --bucket "${BUCKET_NAME}" \
            --region "${AWS_REGION}" \
            --create-bucket-configuration \
            LocationConstraint="${AWS_REGION}"

    fi

    echo "S3 bucket created."

fi

###############################################################################
# Enable Versioning
###############################################################################

echo
echo "Enabling S3 versioning..."

aws s3api put-bucket-versioning \
    --bucket "${BUCKET_NAME}" \
    --versioning-configuration Status=Enabled

echo "S3 versioning enabled."

###############################################################################
# Enable Encryption
###############################################################################

echo
echo "Enabling S3 encryption..."

aws s3api put-bucket-encryption \
    --bucket "${BUCKET_NAME}" \
    --server-side-encryption-configuration \
    '{
        "Rules": [
            {
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "AES256"
                },
                "BucketKeyEnabled": true
            }
        ]
    }'

echo "S3 encryption enabled."

###############################################################################
# Block Public Access
###############################################################################

echo
echo "Blocking public access..."

aws s3api put-public-access-block \
    --bucket "${BUCKET_NAME}" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "S3 public access blocked."

###############################################################################
# Object Ownership
###############################################################################

echo
echo "Configuring S3 Object Ownership..."

aws s3api put-bucket-ownership-controls \
    --bucket "${BUCKET_NAME}" \
    --ownership-controls \
    'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'

echo "Object ownership configured."

###############################################################################
# Create DynamoDB Table
###############################################################################

echo
echo "============================================================"
echo " Creating DynamoDB Lock Table"
echo "============================================================"

if aws dynamodb describe-table \
    --table-name "${DYNAMODB_TABLE}" \
    --region "${AWS_REGION}" >/dev/null 2>&1; then

    echo "DynamoDB table already exists: ${DYNAMODB_TABLE}"

else

    echo "Creating DynamoDB table: ${DYNAMODB_TABLE}"

    aws dynamodb create-table \
        --table-name "${DYNAMODB_TABLE}" \
        --attribute-definitions \
        AttributeName=LockID,AttributeType=S \
        --key-schema \
        AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "${AWS_REGION}"

    echo "Waiting for DynamoDB table..."

    aws dynamodb wait table-exists \
        --table-name "${DYNAMODB_TABLE}" \
        --region "${AWS_REGION}"

    echo "DynamoDB table is ACTIVE."

fi

###############################################################################
# Enable DynamoDB Point-in-Time Recovery
###############################################################################

echo
echo "Enabling DynamoDB Point-in-Time Recovery..."

aws dynamodb update-continuous-backups \
    --table-name "${DYNAMODB_TABLE}" \
    --point-in-time-recovery-specification \
    PointInTimeRecoveryEnabled=true \
    --region "${AWS_REGION}"

echo "DynamoDB Point-in-Time Recovery enabled."

###############################################################################
# Verify S3
###############################################################################

echo
echo "============================================================"
echo " Verifying S3"
echo "============================================================"

VERSIONING=$(aws s3api get-bucket-versioning \
    --bucket "${BUCKET_NAME}" \
    --query 'Status' \
    --output text)

ENCRYPTION=$(aws s3api get-bucket-encryption \
    --bucket "${BUCKET_NAME}" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
    --output text)

echo "Bucket     : ${BUCKET_NAME}"
echo "Versioning : ${VERSIONING}"
echo "Encryption : ${ENCRYPTION}"

###############################################################################
# Verify DynamoDB
###############################################################################

echo
echo "============================================================"
echo " Verifying DynamoDB"
echo "============================================================"

TABLE_STATUS=$(aws dynamodb describe-table \
    --table-name "${DYNAMODB_TABLE}" \
    --region "${AWS_REGION}" \
    --query 'Table.TableStatus' \
    --output text)

TABLE_ARN=$(aws dynamodb describe-table \
    --table-name "${DYNAMODB_TABLE}" \
    --region "${AWS_REGION}" \
    --query 'Table.TableArn' \
    --output text)

echo "Table  : ${DYNAMODB_TABLE}"
echo "Status : ${TABLE_STATUS}"
echo "ARN    : ${TABLE_ARN}"

###############################################################################
# Final Output
###############################################################################

echo
echo "============================================================"
echo " Terraform Backend Ready"
echo "============================================================"
echo
echo "Add this to your Terraform configuration:"
echo
echo 'terraform {'
echo '  backend "s3" {'
echo '    bucket         = "bokiti123"'
echo '    key            = "terraform.tfstate"'
echo '    region         = "us-east-1"'
echo '    encrypt        = true'
echo '    dynamodb_table = "family_dyning"'
echo '  }'
echo '}'
echo
echo "============================================================"
echo " SUCCESS"
echo "============================================================"