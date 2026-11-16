#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
AI_API_URL="${AI_API_URL:-http://localhost:8001}"
APPLICATION_ID="${GATEWAY_APPLICATION_ID:?Set GATEWAY_APPLICATION_ID to a seeded application UUID}"

AUTH_ARGS=()
if [[ -n "${SUPABASE_ACCESS_TOKEN:-}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}")
fi

curl --fail-with-body --silent --show-error "${API_URL}/health"
printf '\n'

REQUEST_JSON="$(curl --fail-with-body --silent --show-error "${AUTH_ARGS[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"application_id\":\"${APPLICATION_ID}\",\"prompt\":\"Return a concise offline smoke-test answer.\",\"router\":\"database\",\"aggregator\":\"none\"}" \
  "${AI_API_URL}/api/v1/gateway/requests")"
REQUEST_ID="$(python -c 'import json,sys; print(json.load(sys.stdin)["request_id"])' <<<"${REQUEST_JSON}")"

printf 'request_id=%s\n' "${REQUEST_ID}"
printf '%s\n' "${REQUEST_JSON}"
curl --fail-with-body --silent --show-error "${AUTH_ARGS[@]}" \
  "${API_URL}/api/v1/gateway/requests/${REQUEST_ID}/traces"
printf '\n'
