#!/usr/bin/env bash
#
# Download the OpenAPI 3.0.3 specification from the (IAM-protected) proxy and publish it
# on OpenAPI Viewer (https://www.openapiviewer.com).
#
# Required environment variable:
#   PROXY_URL   Base URL of the deployed proxy, e.g. https://ai-companion-proxy-xxxx.a.run.app
#
set -euo pipefail

PROXY_URL="${PROXY_URL:?Set PROXY_URL to the deployed proxy base URL}"
SPEC_URL="${PROXY_URL%/}/openapi.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_FILE="${SCRIPT_DIR}/openapi.json"

echo "Downloading ${SPEC_URL} to ${OUTPUT_FILE}"

TOKEN="$(gcloud auth print-identity-token)"
curl -s -H "Authorization: Bearer ${TOKEN}" "${SPEC_URL}" -o "${OUTPUT_FILE}"

python3 -c "
import json
data = json.load(open('${OUTPUT_FILE}', encoding='utf-8-sig'))
print(f'OpenAPI {data.get(\"openapi\")} downloaded: {data.get(\"info\", {}).get(\"title\")}')
"

echo "Uploading to OpenAPI Viewer..."
RESPONSE="$(curl --progress-bar -X POST -F "file=@${OUTPUT_FILE};type=application/json;charset=utf-8" https://www.openapiviewer.com/api/upload)"
echo "Response: ${RESPONSE}"

VIEWER_URL="$(python3 -c "
import json
try:
    d = json.loads('''${RESPONSE}''')
    print(d.get('fullPath') or ('https://www.openapiviewer.com/view/' + d.get('id', '')))
except Exception:
    pass
" 2>/dev/null || true)"

if [ -n "${VIEWER_URL}" ]; then
  echo "Interactive documentation: ${VIEWER_URL}"
fi
