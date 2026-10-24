#!/usr/bin/env bash
# Runs Terraform from its official image, so contributors and CI need only Docker.
# Usage: infra/tf.sh <terraform args...>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
exec docker run --rm -i \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e TF_IN_AUTOMATION=1 \
  -e ARM_CLIENT_ID -e ARM_TENANT_ID -e ARM_SUBSCRIPTION_ID -e ARM_USE_OIDC \
  -e ARM_OIDC_TOKEN -e ARM_OIDC_REQUEST_TOKEN -e ARM_OIDC_REQUEST_URL -e ARM_ENVIRONMENT \
  -v "$here/terraform:/tf" -w /tf \
  "hashicorp/terraform:${TF_VERSION:-1.15}" "$@"
