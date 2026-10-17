# Disaster Recovery Runbook

**Scope:** Full cluster loss or RDS data loss. Target RTO: 2 hours. Target RPO: 24 hours (daily pg_dump to S3).

---

## Scenarios

| Scenario | RTO | RPO | Section |
|---|---|---|---|
| Full cluster destroyed | ~90 min | 24h | §1 |
| RDS instance lost / corrupted | ~30 min | 24h | §2 |
| Accidental LiteLLM spend-table deletion | ~15 min | 24h | §2B |
| GPU node failure (single node) | ~5 min | 0 (stateless) | §3 |
| Secrets rotated / lost | ~10 min | 0 (re-generate) | §4 |

---

## §1 — Full Cluster Rebuild

### Prerequisites
- AWS credentials with EKS/EC2/RDS/S3 access
- Terraform state bucket accessible (`provision/eks/backend.tf`)
- `.env.secrets` file (or secrets already in AWS Secrets Manager)

### Steps

```bash
# 1. Reprovision infrastructure (~15 min)
cd provision/eks
terraform init
terraform apply -auto-approve

# 2. Update kubeconfig
aws eks update-kubeconfig --region us-east-1 --name llm-inference

# 3. Verify nodes
kubectl get nodes

# 4. Deploy in order (same as initial setup)
make deploy-base
make deploy-karpenter
make deploy-infra
make deploy-tracing
make deploy-eso          # restores secrets from Secrets Manager automatically
make deploy-argocd       # Argo CD re-syncs all apps from GitHub
make deploy-waf
make deploy-rollouts

# 5. Trigger Argo CD full sync (Argo CD will deploy vLLM, SGLang, LiteLLM, monitoring)
make sync

# 6. Wait for vLLM model load (up to 10 min per pod)
kubectl get pods -n llm-serving -w

# 7. End-to-end verify
make health-check
```

**Expected total time:** ~90 minutes (dominated by model download into NVMe cache on new nodes).

---

## §2 — RDS Instance Recovery

### A. Restore from automated RDS snapshot (last 7 days)

```bash
# List available snapshots
aws rds describe-db-snapshots \
  --db-instance-identifier llm-inference-litellm \
  --query 'DBSnapshots[*].[DBSnapshotIdentifier,SnapshotCreateTime,Status]' \
  --output table

# Restore to a new instance (change SNAPSHOT_ID and NEW_INSTANCE_ID)
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier llm-inference-litellm-restored \
  --db-snapshot-identifier <SNAPSHOT_ID> \
  --db-instance-class db.t3.medium \
  --db-subnet-group-name llm-inference-rds \
  --no-publicly-accessible

# Wait for available (~10 min)
aws rds wait db-instance-available \
  --db-instance-identifier llm-inference-litellm-restored

# Get new endpoint
aws rds describe-db-instances \
  --db-instance-identifier llm-inference-litellm-restored \
  --query 'DBInstances[0].Endpoint.Address' --output text
```

Then update the `DATABASE_URL` in AWS Secrets Manager and restart LiteLLM:

```bash
aws secretsmanager put-secret-value \
  --secret-id /llm-inference/litellm \
  --secret-string "$(aws secretsmanager get-secret-value \
    --secret-id /llm-inference/litellm \
    --query SecretString --output text | \
    jq --arg url "postgresql://litellm:<PASSWORD>@<NEW_ENDPOINT>/litellm" '.DATABASE_URL = $url')"

kubectl rollout restart deployment/litellm-proxy -n llm-gateway
```

### B. Restore from S3 pg_dump backup (granular table recovery)

```bash
# List available backups
aws s3 ls s3://$(cd provision/eks && terraform output -raw pg_backup_bucket)/postgres/ \
  --recursive | sort | tail -20

# Download and restore a specific backup
BACKUP_KEY="postgres/2026-10-16T03-00-00Z.sql.gz"
aws s3 cp "s3://$(cd provision/eks && terraform output -raw pg_backup_bucket)/$BACKUP_KEY" - | \
  gunzip | \
  kubectl exec -i -n llm-gateway \
    $(kubectl get pod -n llm-gateway -l app=postgres -o jsonpath='{.items[0].metadata.name}') \
    -- psql -U litellm litellm
```

---

## §3 — GPU Node Failure

GPU pods are stateless — model weights are on NVMe hostPath per node. On node failure:

1. Karpenter detects unschedulable GPU pods and provisions a replacement node (~90s).
2. The `model-pull` init container re-downloads model weights to the new node's NVMe.
3. `startupProbe` (failureThreshold: 40, periodSeconds: 15 = 10 min budget) waits for load.
4. Pod becomes Ready and KEDA routes traffic.

**No manual action required** unless Karpenter itself is broken. To verify:

```bash
kubectl get nodes -l workload-type=gpu
kubectl describe nodeclaim -n karpenter   # shows provisioning events
kubectl get pods -n llm-serving           # should recover within 12 min
```

---

## §4 — Secret Rotation / Loss

If AWS Secrets Manager values were corrupted or `.env.secrets` was lost:

```bash
# Regenerate all platform secrets
make secrets
# This runs scripts/generate-secrets.sh which prints new values and prompts to push to Secrets Manager.

# After pushing new values, ESO will sync within 1 hour.
# Force immediate sync:
kubectl annotate externalsecret platform-secrets -n llm-serving \
  force-sync=$(date +%s) --overwrite
kubectl annotate externalsecret litellm-secrets -n llm-gateway \
  force-sync=$(date +%s) --overwrite

# Restart affected deployments to pick up new Secret values
kubectl rollout restart deployment/litellm-proxy -n llm-gateway
kubectl rollout restart deployment/vllm-llama3   -n llm-serving
```

---

## §5 — DR Drill (run quarterly)

Use `scripts/dr-test.sh` to simulate and verify recovery without affecting production:

```bash
# Dry-run: verify all DR preconditions (snapshots exist, S3 backups exist, ESO syncing)
bash scripts/dr-test.sh --dry-run

# Full drill in a separate AWS account / region (non-destructive to prod)
bash scripts/dr-test.sh --region us-west-2
```

The script checks:
- RDS automated snapshot exists and is < 25 hours old
- S3 pg_dump backup exists and is < 25 hours old
- Secrets Manager secrets are populated (no `REPLACE_AFTER_APPLY` values)
- ESO `ExternalSecret` status is `SecretSynced`
- Argo CD applications are `Synced` and `Healthy`

Record drill results in the incident log. Target: all checks green within 10 minutes.
