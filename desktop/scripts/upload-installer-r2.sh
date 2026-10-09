#!/usr/bin/env bash
# Copies a published installer into the hub's R2 installer bucket
# (android/hub/src/installer-store.ts), at
#
#   installers/<version>/<platform>/<object name>
#
# with its SHA-256 as the R2 checksum (R2 refuses the upload if the bytes do not
# match it) and as the sha256 metadata field. The hub serves the object only
# when its size and SHA-256 match the published installer row; otherwise it
# streams the GitHub release asset as before.
#
#   upload-installer-r2.sh <win|mac|linux|android> <version> <file> [object name]
#
# The object name defaults to the file's name, which is what the publisher
# records for mac, linux and android. The Windows installer the hub serves at
# /v1/desktop/installer is stored as AI-Shuffle-Setup.exe.
#
# Environment: R2_INSTALLER_BUCKET, CLOUDFLARE_ACCOUNT_ID, and an R2 API token's
# S3 credentials in R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY (Object Read &
# Write on that bucket only). Needs the AWS CLI (preinstalled on GitHub's
# ubuntu runners).
set -euo pipefail

platform=${1:-} version=${2:-} file=${3:-}
object=${4:-$(basename "$file")}
case "$platform" in win|mac|linux|android) ;; *) echo "usage: $0 <win|mac|linux|android> <version> <file> [object name]" >&2; exit 2;; esac
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must be X.Y.Z, got '$version'" >&2; exit 2; }
[[ -f "$file" ]] || { echo "No such file: $file" >&2; exit 2; }
[[ "$object" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Unexpected object name: $object" >&2; exit 2; }
: "${R2_INSTALLER_BUCKET:?Set R2_INSTALLER_BUCKET to the bucket name}"
: "${CLOUDFLARE_ACCOUNT_ID:?Set CLOUDFLARE_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?Set R2_ACCESS_KEY_ID (R2 API token, S3 access key id)}"
: "${R2_SECRET_ACCESS_KEY:?Set R2_SECRET_ACCESS_KEY (R2 API token, S3 secret access key)}"

key="installers/$version/$platform/$object"
sha256=$(sha256sum "$file" | cut -d' ' -f1)
checksum=$(openssl dgst -sha256 -binary "$file" | base64)
endpoint="https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com"

export AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY AWS_DEFAULT_REGION=auto
# R2 takes the explicit SHA-256 below; skip the CLI's own default checksums.
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
aws s3api put-object --endpoint-url "$endpoint" --bucket "$R2_INSTALLER_BUCKET" --key "$key" \
  --body "$file" --checksum-sha256 "$checksum" --metadata "sha256=$sha256" \
  --content-type application/octet-stream --output text --query ETag > /dev/null
echo "Uploaded $key ($(stat -c %s "$file") bytes, SHA-256 $sha256) to R2 bucket $R2_INSTALLER_BUCKET"
