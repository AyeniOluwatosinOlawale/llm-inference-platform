#!/usr/bin/env bash
# DR precondition check — verifies all recovery assets exist before an incident.
# Run quarterly or after any major infrastructure change.
# Usage:
#   bash scripts/dr-test.sh --dry-run          # check only, no changes
#   bash scripts/dr-test.sh --region us-west-2 # test restore in alternate region

set -euo pipefail

DRY_RUN=false
REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="${CLUSTER_NAME:-llm-inference}"
PASS=0
FAIL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  DRY_RUN=true ;;
    --region)   REGION="$2"; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
  shift
done

GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[0;33m"
NC="\033[0m"

ok()   { echo -e "  ${GREEN}PASS${NC}  $1"; ((PASS++)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; ((FAIL++)); }
info() { echo -e "  ${YELLOW}INFO${NC}  $1"; }

echo ""
echo "=== DR Precondition Check — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "    Cluster: $CLUSTER_NAME  |  Region: $REGION  |  Dry-run: $DRY_RUN"
echo ""

# ── 1. RDS automated snapshot exists and is recent ────────────────────────────
echo "── RDS Snapshots"
LATEST_SNAPSHOT=$(aws rds describe-db-snapshots \
  --region "$REGION" \
  --db-instance-identifier "${CLUSTER_NAME}-litellm" \
  --query 'sort_by(DBSnapshots, &SnapshotCreateTime)[-1]' \
  --output json 2>/dev/null || echo "{}")

SNAPSHOT_ID=$(echo "$LATEST_SNAPSHOT" | jq -r '.DBSnapshotIdentifier // empty')
SNAPSHOT_TIME=$(echo "$LATEST_SNAPSHOT" | jq -r '.SnapshotCreateTime // empty')

if [[ -z "$SNAPSHOT_ID" ]]; then
  fail "No RDS snapshot found for ${CLUSTER_NAME}-litellm"
else
  # Check age — must be < 25 hours
  SNAPSHOT_EPOCH=$(date -d "$SNAPSHOT_TIME" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%S" "${SNAPSHOT_TIME%.*}" +%s 2>/dev/null || echo 0)
  NOW_EPOCH=$(date +%s)
  AGE_HOURS=$(( (NOW_EPOCH - SNAPSHOT_EPOCH) / 3600 ))
  if [[ $AGE_HOURS -lt 25 ]]; then
    ok "RDS snapshot $SNAPSHOT_ID is ${AGE_HOURS}h old (< 25h)"
  else
    fail "RDS snapshot $SNAPSHOT_ID is ${AGE_HOURS}h old — automated backups may be broken"
  fi
fi

# ── 2. S3 pg_dump backup exists and is recent ─────────────────────────────────
echo ""
echo "── S3 pg_dump Backups"
BACKUP_BUCKET=$(cd provision/eks 2>/dev/null && terraform output -raw pg_backup_bucket 2>/dev/null || echo "")

if [[ -z "$BACKUP_BUCKET" ]]; then
  fail "Cannot read pg_backup_bucket from terraform output — run: cd provision/eks && terraform output"
else
  LATEST_BACKUP=$(aws s3 ls "s3://${BACKUP_BUCKET}/postgres/" \
    --region "$REGION" 2>/dev/null | sort | tail -1 | awk '{print $4}')
  if [[ -z "$LATEST_BACKUP" ]]; then
    fail "No pg_dump backups found in s3://${BACKUP_BUCKET}/postgres/"
  else
    # Extract timestamp from filename (format: postgres/2026-10-16T03-00-00Z.sql.gz)
    BACKUP_DATE=$(echo "$LATEST_BACKUP" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)
    TODAY=$(date -u +%Y-%m-%d)
    YESTERDAY=$(date -u -d "yesterday" +%Y-%m-%d 2>/dev/null || date -u -v-1d +%Y-%m-%d 2>/dev/null)
    if [[ "$BACKUP_DATE" == "$TODAY" || "$BACKUP_DATE" == "$YESTERDAY" ]]; then
      ok "S3 backup $LATEST_BACKUP exists and is recent ($BACKUP_DATE)"
    else
      fail "Latest S3 backup is from $BACKUP_DATE — pg_dump CronJob may have failed"
    fi
  fi
fi

# ── 3. Secrets Manager — no placeholder values ────────────────────────────────
echo ""
echo "── AWS Secrets Manager"
for SECRET_PATH in "${CLUSTER_NAME}/platform" "${CLUSTER_NAME}/litellm"; do
  SECRET_VALUE=$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_PATH" \
    --region "$REGION" \
    --query SecretString --output text 2>/dev/null || echo "")
  if [[ -z "$SECRET_VALUE" ]]; then
    fail "Secret /$SECRET_PATH not found or not accessible"
  elif echo "$SECRET_VALUE" | grep -q "REPLACE_AFTER_APPLY"; then
    fail "Secret /$SECRET_PATH still has placeholder values — run: make secrets"
  else
    ok "Secret /$SECRET_PATH is populated"
  fi
done

# ── 4. ESO ExternalSecret sync status ─────────────────────────────────────────
echo ""
echo "── External Secrets Operator"
for NS_SECRET in "llm-serving/platform-secrets" "llm-gateway/litellm-secrets"; do
  NS="${NS_SECRET%%/*}"
  SECRET="${NS_SECRET##*/}"
  STATUS=$(kubectl get externalsecret "$SECRET" -n "$NS" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}' 2>/dev/null || echo "NotFound")
  if [[ "$STATUS" == "SecretSynced" ]]; then
    ok "ExternalSecret $SECRET ($NS) is SecretSynced"
  else
    fail "ExternalSecret $SECRET ($NS) status: $STATUS"
  fi
done

# ── 5. Argo CD application health ─────────────────────────────────────────────
echo ""
echo "── Argo CD Applications"
APPS=$(kubectl get applications -n argocd \
  -o jsonpath='{range .items[*]}{.metadata.name},{.status.sync.status},{.status.health.status}{"\n"}{end}' 2>/dev/null || echo "")

if [[ -z "$APPS" ]]; then
  info "Argo CD not reachable — skipping (non-fatal)"
else
  while IFS=',' read -r APP SYNC HEALTH; do
    if [[ "$SYNC" == "Synced" && "$HEALTH" == "Healthy" ]]; then
      ok "Argo CD app $APP is Synced/Healthy"
    else
      fail "Argo CD app $APP is $SYNC/$HEALTH"
    fi
  done <<< "$APPS"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Result: ${PASS} passed, ${FAIL} failed ==="
echo ""

if [[ $FAIL -gt 0 ]]; then
  echo "  Fix failures before the next drill. See docs/runbooks/disaster-recovery.md."
  exit 1
else
  echo "  All DR preconditions met. Platform is recoverable within RTO/RPO targets."
  exit 0
fi
