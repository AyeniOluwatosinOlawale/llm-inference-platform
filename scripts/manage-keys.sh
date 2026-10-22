#!/usr/bin/env bash
# manage-keys.sh — create, list, and delete LiteLLM user API keys
# Usage:
#   ./scripts/manage-keys.sh create --user alice --budget 5 --rpm 20
#   ./scripts/manage-keys.sh list
#   ./scripts/manage-keys.sh delete sk-xxxx
#   ./scripts/manage-keys.sh info sk-xxxx
#
# Required env: LITELLM_MASTER_KEY, LITELLM_URL (default: https://api.136.65.123.94.nip.io)

set -euo pipefail

LITELLM_URL="${LITELLM_URL:-https://api.136.65.123.94.nip.io}"
MASTER_KEY="${LITELLM_MASTER_KEY:?Set LITELLM_MASTER_KEY env var}"

cmd="${1:-help}"
shift || true

create_key() {
  local user="" budget="5" rpm="30" duration="30d" models="AnaModel,qwen3"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --user)    user="$2";    shift 2 ;;
      --budget)  budget="$2";  shift 2 ;;
      --rpm)     rpm="$2";     shift 2 ;;
      --duration) duration="$2"; shift 2 ;;
      --models)  models="$2";  shift 2 ;;
      *) echo "Unknown flag: $1"; exit 1 ;;
    esac
  done

  [[ -z "$user" ]] && { echo "Error: --user is required"; exit 1; }

  # Build JSON models array
  IFS=',' read -ra model_arr <<< "$models"
  model_json=$(printf '"%s",' "${model_arr[@]}")
  model_json="[${model_json%,}]"

  echo "Creating key for user: $user (budget: \$$budget/${duration}, rpm: $rpm)..."

  curl -s -X POST "${LITELLM_URL}/key/generate" \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{
      \"user_id\": \"${user}\",
      \"max_budget\": ${budget},
      \"budget_duration\": \"${duration}\",
      \"rpm_limit\": ${rpm},
      \"models\": ${model_json},
      \"metadata\": {\"created_by\": \"manage-keys.sh\", \"created_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
    }" | jq '{
      key: .key,
      user_id: .user_id,
      max_budget: .max_budget,
      rpm_limit: .rpm_limit,
      models: .models
    }'
}

list_keys() {
  echo "Listing all keys..."
  curl -s "${LITELLM_URL}/v2/key/list" \
    -H "Authorization: Bearer ${MASTER_KEY}" | jq '.keys[] | {key: .token, user_id: .user_id, spend: .spend, max_budget: .max_budget, rpm_limit: .rpm_limit}'
}

delete_key() {
  local key="${1:?Provide the key to delete}"
  echo "Deleting key: ${key:0:12}..."
  curl -s -X DELETE "${LITELLM_URL}/key/delete" \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"keys\": [\"${key}\"]}" | jq .
}

key_info() {
  local key="${1:?Provide the key}"
  curl -s "${LITELLM_URL}/key/info?key=${key}" \
    -H "Authorization: Bearer ${MASTER_KEY}" | jq .
}

case "$cmd" in
  create) create_key "$@" ;;
  list)   list_keys ;;
  delete) delete_key "$@" ;;
  info)   key_info "$@" ;;
  help|*)
    echo "Usage: manage-keys.sh <create|list|delete|info> [options]"
    echo ""
    echo "  create --user NAME [--budget N] [--rpm N] [--duration Nd] [--models m1,m2]"
    echo "  list"
    echo "  delete SK-KEY"
    echo "  info SK-KEY"
    ;;
esac
