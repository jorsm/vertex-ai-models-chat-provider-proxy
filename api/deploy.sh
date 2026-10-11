#!/usr/bin/env bash
#
# Build and deploy the Google Agent Platform for Copilot Chat - Proxy Cloud Run service with Cloud Build.
#
# Required environment variables:
#   PROJECT_ID   Google Cloud project hosting the service (the hub project)
#   REGION       Cloud Run region of the service
# Optional:
#   SERVICE_NAME Cloud Run service name (default: copilot-chat-proxy)
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to the hub project ID}"
REGION="${REGION:?Set REGION to the Cloud Run region}"
SERVICE_NAME="${SERVICE_NAME:-copilot-chat-proxy}"

if ! command -v gcloud &>/dev/null; then
  echo "Error: Google Cloud SDK (gcloud) not found in PATH." >&2
  exit 1
fi

# The api directory is the Cloud Build source context and contains the Dockerfile.
API_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "${API_DIR}/Dockerfile" ] || [ ! -f "${API_DIR}/requirements.txt" ] || [ ! -f "${API_DIR}/src/main.py" ]; then
  echo "Error: incomplete API context in ${API_DIR} (expected Dockerfile, requirements.txt and src/main.py)." >&2
  exit 1
fi

echo "Building and deploying '${SERVICE_NAME}' (${PROJECT_ID} / ${REGION})..."
gcloud run deploy "${SERVICE_NAME}" \
  --source="${API_DIR}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')"
echo "Deployment complete: ${SERVICE_URL}"
