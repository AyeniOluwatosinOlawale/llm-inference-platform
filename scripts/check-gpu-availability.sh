#!/bin/bash
# Checks GPU availability across GCP regions and spins up the cluster if found.
# Runs every 15 minutes via ScheduleWakeup loop.

PROJECT="testing-494308"
CLUSTER="llm-inference"
CURRENT_REGION="europe-west4"
REGIONS=("europe-west4" "europe-west1" "us-central1" "us-east1")
LOG="/tmp/gpu-availability.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== GPU availability check started ==="

# 1. Try current cluster region first (cheapest — just resize existing pool)
log "Trying $CURRENT_REGION (existing cluster)..."
RESULT=$(gcloud container clusters resize "$CLUSTER" \
  --node-pool=gpu-nodes \
  --num-nodes=1 \
  --region="$CURRENT_REGION" \
  --project="$PROJECT" \
  --quiet 2>&1)

if echo "$RESULT" | grep -q "done\|Updated"; then
  log "SUCCESS: GPU node provisioned in $CURRENT_REGION!"

  # Scale CPU nodes back up
  log "Scaling CPU nodes back to 2..."
  gcloud container clusters resize "$CLUSTER" \
    --node-pool=cpu-nodes \
    --num-nodes=2 \
    --region="$CURRENT_REGION" \
    --project="$PROJECT" \
    --quiet 2>&1 && log "CPU nodes scaled up."

  log "Cluster is coming back online. vLLM will start once nodes are Ready."
  echo "GPU_FOUND=true" > /tmp/gpu-status.env
  echo "GPU_REGION=$CURRENT_REGION" >> /tmp/gpu-status.env
  exit 0
else
  log "europe-west4 unavailable: $(echo "$RESULT" | grep -i 'error\|quota\|stockout' | head -1)"
fi

# 2. Check other regions for GPU quota (informational — new cluster needed)
for REGION in "${REGIONS[@]:1}"; do
  log "Checking GPU quota in $REGION..."
  QUOTA=$(gcloud compute regions describe "$REGION" \
    --project="$PROJECT" \
    --format="value(quotas[].metric,quotas[].limit)" 2>/dev/null | \
    grep -A1 "NVIDIA_L4_GPUS\|NVIDIA_T4_GPUS" | \
    grep -v "PREEMPTIBLE\|COMMITTED" | head -4)
  log "$REGION quotas: $QUOTA"
done

log "No GPU available yet. Will check again in 15 minutes."
echo "GPU_FOUND=false" > /tmp/gpu-status.env
exit 1
