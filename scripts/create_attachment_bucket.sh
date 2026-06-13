#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/create_attachment_bucket.sh

Creates or updates an S3 bucket for EDCom transactional attachment storage.

Environment:
  EDCOM_ATTACHMENT_BUCKET
      Bucket name to create/configure. Defaults to EDCOM_ATTACHMENT_TEST_BUCKET.

  EDCOM_ATTACHMENT_REGION
      AWS region for the bucket. Defaults to EDCOM_ATTACHMENT_TEST_REGION,
      then EDCOM_SES_REGION, then `aws configure get region`.

  EDCOM_ATTACHMENT_PREFIX
      Object prefix for lifecycle management. Defaults to EDCOM_ATTACHMENT_TEST_PREFIX,
      then attachments/txn/.

  EDCOM_ATTACHMENT_LIFECYCLE_EXPIRATION_DAYS
      Expire completed attachment objects after this many days. Defaults to 2.

  EDCOM_ATTACHMENT_LIFECYCLE_ABORT_MULTIPART_DAYS
      Abort incomplete multipart uploads after this many days. Defaults to 1.

  EDCOM_ATTACHMENT_ENDPOINT_URL
      Optional S3-compatible endpoint URL. Defaults to EDCOM_ATTACHMENT_TEST_ENDPOINT_URL.
      Leave unset for AWS S3.

  EDCOM_ATTACHMENT_SKIP_CREATE
      Set to true to skip bucket creation and only apply configuration.

Examples:
  EDCOM_ATTACHMENT_BUCKET="$EDCOM_ATTACHMENT_TEST_BUCKET" \
  EDCOM_ATTACHMENT_REGION=us-west-2 \
  scripts/create_attachment_bucket.sh

  EDCOM_ATTACHMENT_BUCKET=edcom-attachments-prod \
  EDCOM_ATTACHMENT_REGION=us-west-2 \
  scripts/create_attachment_bucket.sh
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

bool_enabled() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

json_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    printf '%s' "$value"
}

bucket="${EDCOM_ATTACHMENT_BUCKET:-${EDCOM_ATTACHMENT_TEST_BUCKET:-}}"
region="${EDCOM_ATTACHMENT_REGION:-${EDCOM_ATTACHMENT_TEST_REGION:-${EDCOM_SES_REGION:-}}}"
prefix="${EDCOM_ATTACHMENT_PREFIX:-${EDCOM_ATTACHMENT_TEST_PREFIX:-attachments/txn/}}"
expiration_days="${EDCOM_ATTACHMENT_LIFECYCLE_EXPIRATION_DAYS:-2}"
abort_multipart_days="${EDCOM_ATTACHMENT_LIFECYCLE_ABORT_MULTIPART_DAYS:-1}"
endpoint_url="${EDCOM_ATTACHMENT_ENDPOINT_URL:-${EDCOM_ATTACHMENT_TEST_ENDPOINT_URL:-}}"
skip_create="${EDCOM_ATTACHMENT_SKIP_CREATE:-false}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 1
            ;;
    esac
done

require_command aws

if [[ -z "$bucket" ]]; then
    die "EDCOM_ATTACHMENT_BUCKET or EDCOM_ATTACHMENT_TEST_BUCKET is required"
fi

if [[ -z "$region" ]]; then
    region="$(aws configure get region || true)"
fi
if [[ -z "$region" ]]; then
    die "EDCOM_ATTACHMENT_REGION, EDCOM_ATTACHMENT_TEST_REGION, or AWS default region is required"
fi

if [[ "$prefix" == /* ]]; then
    die "attachment prefix must be relative, got: $prefix"
fi
if [[ "$prefix" != */ ]]; then
    prefix="$prefix/"
fi

if ! [[ "$expiration_days" =~ ^[0-9]+$ ]] || [[ "$expiration_days" -lt 1 ]]; then
    die "expiration days must be a positive integer"
fi
if ! [[ "$abort_multipart_days" =~ ^[0-9]+$ ]] || [[ "$abort_multipart_days" -lt 1 ]]; then
    die "abort multipart days must be a positive integer"
fi

aws_args=(--region "$region")
if [[ -n "$endpoint_url" ]]; then
    aws_args+=(--endpoint-url "$endpoint_url")
fi

echo "Attachment bucket: $bucket"
echo "Region:            $region"
echo "Prefix:            $prefix"
if [[ -n "$endpoint_url" ]]; then
    echo "Endpoint URL:      $endpoint_url"
fi

if aws "${aws_args[@]}" s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    echo "Bucket exists and is accessible."
elif bool_enabled "$skip_create"; then
    die "bucket does not exist or is inaccessible and EDCOM_ATTACHMENT_SKIP_CREATE=true"
else
    echo "Creating bucket..."
    if [[ "$region" == "us-east-1" || "$region" == "auto" ]]; then
        aws "${aws_args[@]}" s3api create-bucket --bucket "$bucket"
    else
        aws "${aws_args[@]}" s3api create-bucket \
            --bucket "$bucket" \
            --create-bucket-configuration "LocationConstraint=$region"
    fi
fi

echo "Blocking public access..."
aws "${aws_args[@]}" s3api put-public-access-block \
    --bucket "$bucket" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "Setting bucket ownership controls..."
aws "${aws_args[@]}" s3api put-bucket-ownership-controls \
    --bucket "$bucket" \
    --ownership-controls '{
        "Rules": [
            {
                "ObjectOwnership": "BucketOwnerEnforced"
            }
        ]
    }'

echo "Setting default encryption..."
aws "${aws_args[@]}" s3api put-bucket-encryption \
    --bucket "$bucket" \
    --server-side-encryption-configuration '{
        "Rules": [
            {
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "AES256"
                },
                "BucketKeyEnabled": true
            }
        ]
    }'

lifecycle_file="$(mktemp)"
cleanup() {
    rm -f "$lifecycle_file"
}
trap cleanup EXIT

escaped_prefix="$(json_escape "$prefix")"
cat > "$lifecycle_file" <<JSON
{
  "Rules": [
    {
      "ID": "edcom-transactional-attachment-expiration",
      "Status": "Enabled",
      "Filter": {
        "Prefix": "$escaped_prefix"
      },
      "Expiration": {
        "Days": $expiration_days
      },
      "AbortIncompleteMultipartUpload": {
        "DaysAfterInitiation": $abort_multipart_days
      }
    }
  ]
}
JSON

echo "Configuring lifecycle..."
aws "${aws_args[@]}" s3api put-bucket-lifecycle-configuration \
    --bucket "$bucket" \
    --lifecycle-configuration "file://$lifecycle_file"

echo "Verifying lifecycle..."
aws "${aws_args[@]}" s3api get-bucket-lifecycle-configuration --bucket "$bucket" \
    --output json

cat <<EOF

Attachment bucket ready.

edcom.json app config:
  "attachment_enabled": "true",
  "attachment_storage_backend": "s3",
  "attachment_s3_bucket": "$bucket",
  "attachment_s3_prefix": "$prefix",
  "attachment_s3_region": "$region",
  "attachment_s3_access_key": "<EDCOM_SES_ACCESS_KEY_ID>",
  "attachment_s3_secret_key": "<EDCOM_SES_SECRET_ACCESS_KEY>",
  "attachment_s3_endpoint_url": "$endpoint_url",
  "attachment_manage_lifecycle": "true",
  "attachment_ttl_hours": "24",
  "attachment_lifecycle_expiration_days": "$expiration_days",
  "attachment_lifecycle_abort_multipart_days": "$abort_multipart_days",
  "attachment_max_count": "5",
  "attachment_max_file_bytes": "10485760",
  "attachment_max_total_bytes": "20971520",
  "attachment_max_mime_bytes": "31457280",
  "attachment_json_body_max_bytes": "33554432"
EOF
