# llm-inference-platform — production LLM serving on AWS EKS / RunPod / Vast.ai
# Engines: vLLM v0.30.0 + SGLang v0.5.20  |  Models: Llama 3.1 8B + Qwen3 8B
# Lab integration: inference-optimizer-lab2 (VLLMClient / SGLangClient)

.PHONY: help secrets up-eks up-runpod up-vastai \
        deploy-base deploy-karpenter deploy-infra deploy-tracing deploy-argocd deploy-waf \
        deploy-rollouts deploy-velero verify-metrics \
        deploy-vllm deploy-sglang deploy-litellm deploy-dashboards deploy-vllm-70b \
        health-check load-test \
        benchmark-lab benchmark-prod benchmark-compare \
        canary promote rollback sync \
        redeploy-vllm redeploy-sglang teardown

# Load secrets if the file exists
-include .env.secrets
export

DNS_ZONE      ?= example.com
LAB_RESULTS   ?= $(shell ls /path/to/inference-optimizer-lab2/results/**/*.json 2>/dev/null | head -1)
PROD_RESULTS  ?= $(shell ls production_benchmark_*.json 2>/dev/null | tail -1)
LAB2_PATH     ?= ../inference-optimizer-lab2

help:
	@echo ""
	@echo "  llm-inference-platform"
	@echo ""
	@echo "  Provision"
	@echo "    make secrets        Generate .env.secrets (run once)"
	@echo "    make up-runpod      Terraform → RunPod managed K8s"
	@echo "    make up-vastai      Python SDK → Vast.ai instances + k3s"
	@echo ""
	@echo "  Deploy (in order)"
	@echo "    make deploy-base      Namespaces + NetworkPolicies"
	@echo "    make deploy-infra     Prometheus, KEDA, Traefik, Loki"
	@echo "    make deploy-tracing   Grafana Tempo + OTel Collector"
	@echo "    make deploy-argocd    Argo CD GitOps (auto-sync every 3 min)"
	@echo "    make deploy-waf       AWS WAF v2 + ALB in front of Traefik"
	@echo "    make deploy-rollouts  Argo Rollouts (canary + Prometheus analysis)"
	@echo "    make deploy-velero    Velero cluster + PVC backup"
	@echo "    make verify-metrics   Confirm vLLM/SGLang metric labels"
	@echo "    make deploy-vllm      Llama3 + Qwen3 on vLLM v0.30.0"
	@echo "    make deploy-sglang    Llama3 + Qwen3 on SGLang v0.5.20"
	@echo "    make deploy-litellm   LiteLLM gateway"
	@echo "    make deploy-dashboards Grafana dashboards"
	@echo ""
	@echo "  Operations"
	@echo "    make health-check   End-to-end platform check"
	@echo "    make load-test      TTFT/TPOT/stress load test"
	@echo "    make redeploy-vllm  Rolling restart vLLM pods"
	@echo "    make redeploy-sglang Rolling restart SGLang pods"
	@echo ""
	@echo "  Benchmarks (lab2 integration)"
	@echo "    make benchmark-prod  Run lab2 scenarios against live platform"
	@echo "    make benchmark-compare Compare lab2 vs production results"
	@echo ""
	@echo "  Teardown"
	@echo "    make teardown       Destroy cluster (confirm first)"

# ── Secrets ────────────────────────────────────────────────────────────────────

secrets:
	@bash scripts/generate-secrets.sh

# ── Provisioning ───────────────────────────────────────────────────────────────

up-eks:
	@echo "==> Provisioning AWS EKS cluster (g5.xlarge GPU nodes)..."
	cd provision/eks && bash bootstrap.sh
	cd provision/eks && terraform init && terraform apply
	aws eks update-kubeconfig --region us-east-1 --name llm-inference
	@echo "    KUBECONFIG updated. Run: kubectl get nodes"

up-runpod:
	@echo "==> Provisioning RunPod K8s cluster..."
	cd provision/runpod && terraform init && terraform apply -var="dns_zone=$(DNS_ZONE)"
	@echo "==> Fetching kubeconfig..."
	cd provision/runpod && terraform output -raw kubeconfig > ../../kubeconfig
	@echo "    export KUBECONFIG=$(PWD)/kubeconfig"

up-vastai:
	@echo "==> Provisioning Vast.ai instances + k3s..."
	cd provision/vastai && python3 vastai.py create
	@echo "    Follow the kubeconfig instructions printed above."

# ── Deploy ─────────────────────────────────────────────────────────────────────

deploy-base:
	kubectl apply -f k8s/base/namespaces.yaml
	kubectl apply -f k8s/base/network-policies/

# ── Karpenter ──────────────────────────────────────────────────────────────────
# Adds fast burst GPU provisioning (~90s) on top of the existing managed node group.
# Run after deploy-base. Values are read from terraform output automatically.
deploy-karpenter:
	@echo "==> Reading Karpenter config from Terraform outputs..."
	$(eval CLUSTER_ENDPOINT    := $(shell cd provision/eks && terraform output -raw cluster_endpoint 2>/dev/null))
	$(eval CONTROLLER_ROLE_ARN := $(shell cd provision/eks && terraform output -raw karpenter_controller_role_arn 2>/dev/null))
	$(eval INTERRUPTION_QUEUE  := $(shell cd provision/eks && terraform output -raw karpenter_interruption_queue_name 2>/dev/null))
	$(eval NODE_PROFILE_ROLE   := $(shell cd provision/eks && terraform output -raw karpenter_node_instance_profile 2>/dev/null))
	@echo "==> Installing Karpenter Helm chart..."
	helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
		--version "1.0.1" \
		--namespace karpenter --create-namespace \
		--values helm/karpenter-values.yaml \
		--set "settings.clusterName=$(CLUSTER_NAME)" \
		--set "settings.clusterEndpoint=$(CLUSTER_ENDPOINT)" \
		--set "settings.interruptionQueue=$(INTERRUPTION_QUEUE)" \
		--set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$(CONTROLLER_ROLE_ARN)"
	@echo "==> Waiting for Karpenter controller to be ready..."
	kubectl rollout status deployment/karpenter -n karpenter --timeout=120s
	@echo "==> Applying EC2NodeClass and NodePool..."
	sed "s|\$${KARPENTER_NODE_INSTANCE_PROFILE_ROLE}|$(NODE_PROFILE_ROLE)|g" \
		k8s/karpenter/ec2-node-class.yaml | kubectl apply -f -
	kubectl apply -f k8s/karpenter/node-pool.yaml
	@echo "==> Karpenter ready. GPU burst nodes will provision on next Pending pod."

deploy-infra:
	@echo "==> Installing Helm charts..."
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo add kedacore https://kedacore.github.io/charts
	helm repo add traefik https://helm.traefik.io/traefik
	helm repo add grafana https://grafana.github.io/helm-charts
	helm repo update
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
		--namespace monitoring --create-namespace \
		--values helm/kube-prometheus-stack-values.yaml \
		--set grafana.adminPassword="$(GRAFANA_ADMIN_PASSWORD)" \
		--set alertmanager.alertmanagerSpec.config.global.slack_api_url="$(SLACK_WEBHOOK_URL)"
	helm upgrade --install keda kedacore/keda \
		--namespace keda --create-namespace \
		--values helm/keda-values.yaml
	helm upgrade --install traefik traefik/traefik \
		--namespace traefik --create-namespace \
		--values helm/traefik-values.yaml
	helm upgrade --install loki-stack grafana/loki-stack \
		--namespace monitoring \
		-f k8s/monitoring/loki/values.yaml \
		--values helm/loki-stack-values.yaml

# Optional: Velero cluster-state + PVC backup. Requires S3 bucket + AWS credentials.
# Skip this target if you don't have AWS set up — Postgres and Redis PVCs still persist locally.
deploy-velero:
	@if [ -z "$(VELERO_BACKUP_BUCKET)" ] || [ "$(VELERO_BACKUP_BUCKET)" = "REPLACE_ME" ]; then \
		echo "Skipping Velero — VELERO_BACKUP_BUCKET not set in .env.secrets."; \
		echo "Postgres and Redis data are still persisted on PVCs."; \
		exit 0; \
	fi
	@echo "==> Installing Velero for cluster-state and PVC backups..."
	helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts
	helm repo update
	helm upgrade --install velero vmware-tanzu/velero \
		--namespace velero --create-namespace \
		--values helm/velero-values.yaml \
		--set configuration.backupStorageLocation[0].config.region="$(AWS_REGION)" \
		--set configuration.backupStorageLocation[0].bucket="$(VELERO_BACKUP_BUCKET)" \
		--set credentials.secretContents.cloud="[default]\naws_access_key_id=$(VELERO_AWS_ACCESS_KEY_ID)\naws_secret_access_key=$(VELERO_AWS_SECRET_ACCESS_KEY)"

verify-metrics:
	@bash scripts/verify-metrics.sh

deploy-vllm:
	@echo "==> Creating platform secret for HuggingFace token..."
	kubectl create secret generic platform-secrets -n llm-serving \
		--from-literal=HUGGINGFACE_TOKEN="$(HUGGINGFACE_TOKEN)" \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f k8s/vllm/llama3/
	kubectl apply -f k8s/vllm/qwen3/
	kubectl apply -f k8s/monitoring/prometheus-rules.yaml
	@echo "==> Waiting for vLLM pods to pass startupProbe (up to 10 min)..."
	kubectl rollout status deployment/vllm-llama3 -n llm-serving --timeout=600s
	kubectl rollout status deployment/vllm-qwen3  -n llm-serving --timeout=600s

deploy-sglang:
	kubectl apply -f k8s/sglang/llama3/
	kubectl apply -f k8s/sglang/qwen3/
	kubectl rollout status deployment/sglang-llama3 -n llm-serving --timeout=600s
	kubectl rollout status deployment/sglang-qwen3  -n llm-serving --timeout=600s

deploy-litellm:
	@echo "==> Injecting LiteLLM secrets and backup config..."
	$(eval RDS_ENDPOINT      := $(shell cd provision/eks && terraform output -raw rds_endpoint 2>/dev/null))
	$(eval RDS_PASSWORD      := $(shell cd provision/eks && terraform output -raw rds_password 2>/dev/null))
	$(eval DB_URL            := $(if $(RDS_ENDPOINT),postgresql://litellm:$(RDS_PASSWORD)@$(RDS_ENDPOINT)/litellm,postgresql://litellm:$(POSTGRES_PASSWORD)@postgres-service.llm-gateway.svc.cluster.local:5432/litellm))
	$(eval PG_BACKUP_BUCKET  := $(shell cd provision/eks && terraform output -raw pg_backup_bucket 2>/dev/null))
	$(eval PG_BACKUP_ROLE    := $(shell cd provision/eks && terraform output -raw pg_backup_role_arn 2>/dev/null))
	kubectl create secret generic litellm-secrets -n llm-gateway \
		--from-literal=LITELLM_MASTER_KEY="$(LITELLM_MASTER_KEY)" \
		--from-literal=REDIS_PASSWORD="$(REDIS_PASSWORD)" \
		--from-literal=POSTGRES_PASSWORD="$(POSTGRES_PASSWORD)" \
		--from-literal=DATABASE_URL="$(DB_URL)" \
		--dry-run=client -o yaml | kubectl apply -f -
	@if [ -z "$(RDS_ENDPOINT)" ]; then \
		echo "    RDS not provisioned — deploying in-cluster Postgres fallback"; \
		sed -e "s|\$${PG_BACKUP_BUCKET}|$(PG_BACKUP_BUCKET)|g" \
		    -e "s|\$${PG_BACKUP_ROLE_ARN}|$(PG_BACKUP_ROLE)|g" \
		    k8s/litellm/postgres.yaml | kubectl apply -f -; \
	else \
		echo "    Using RDS endpoint: $(RDS_ENDPOINT)"; \
	fi
	kubectl apply -f k8s/litellm/redis.yaml
	kubectl apply -f k8s/litellm/config.yaml
	kubectl apply -f k8s/litellm/deployment.yaml
	kubectl apply -f k8s/litellm/hpa.yaml
	kubectl apply -f k8s/litellm/pdb.yaml
	kubectl apply -f k8s/litellm/ingress.yaml
	kubectl rollout status deployment/litellm-proxy -n llm-gateway --timeout=120s

deploy-dashboards:
	kubectl apply -f k8s/monitoring/dashboards/dashboards-configmap.yaml

# ── Distributed Tracing ────────────────────────────────────────────────────────
# Grafana Tempo (backend) + OpenTelemetry Collector (receiver/fan-out).
# LiteLLM and vLLM send OTLP traces → OTel Collector → Tempo → Grafana Explore.
deploy-tracing:
	@echo "==> Installing Grafana Tempo..."
	helm repo add grafana https://grafana.github.io/helm-charts
	helm repo update
	helm upgrade --install tempo grafana/tempo \
		--namespace monitoring --create-namespace \
		--values helm/tempo-values.yaml
	@echo "==> Installing OpenTelemetry Collector..."
	helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
	helm repo update
	helm upgrade --install opentelemetry-collector open-telemetry/opentelemetry-collector \
		--namespace monitoring \
		--values helm/otel-collector-values.yaml
	@echo "==> Tracing stack ready. Grafana Explore → Tempo datasource."

# ── Argo CD GitOps ─────────────────────────────────────────────────────────────
# Self-heals the cluster to match the GitHub repo every 3 minutes.
# After install, apply apps.yaml to register all platform components.
deploy-argocd:
	@echo "==> Installing Argo CD..."
	helm repo add argo https://argoproj.github.io/argo-helm
	helm repo update
	helm upgrade --install argocd argo/argo-cd \
		--namespace argocd --create-namespace \
		--values helm/argocd-values.yaml \
		--set "server.ingress.hosts[0]=argocd.$(DNS_ZONE)" \
		--set "server.ingress.tls[0].hosts[0]=argocd.$(DNS_ZONE)"
	@echo "==> Waiting for Argo CD server..."
	kubectl rollout status deployment/argocd-server -n argocd --timeout=120s
	@echo "==> Registering platform Applications..."
	kubectl apply -f k8s/argocd/project.yaml
	kubectl apply -f k8s/argocd/apps.yaml
	@echo "==> Argo CD ready at https://argocd.$(DNS_ZONE)"
	@echo "    Initial admin password: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"

sync:
	@echo "==> Triggering Argo CD sync for all apps..."
	argocd app sync --selector app.kubernetes.io/part-of=llm-platform 2>/dev/null || \
		kubectl annotate applications -n argocd --all argocd.argoproj.io/refresh=hard

# ── Argo Rollouts ──────────────────────────────────────────────────────────────
# Progressive delivery for vLLM pods: 10% canary → Prometheus analysis → 50% → 100%.
# Auto-rollback if error rate > 1% or p99 TTFT > 3s during any 60s analysis window.
# Run AFTER deploy-infra (needs Prometheus running for AnalysisTemplate metrics).
deploy-rollouts:
	@echo "==> Installing Argo Rollouts controller..."
	helm repo add argo https://argoproj.github.io/argo-helm
	helm repo update
	helm upgrade --install argo-rollouts argo/argo-rollouts \
		--namespace argo-rollouts --create-namespace \
		--values helm/argo-rollouts-values.yaml \
		--set "dashboard.ingress.hosts[0]=rollouts.$(DNS_ZONE)" \
		--set "dashboard.ingress.tls[0].hosts[0]=rollouts.$(DNS_ZONE)"
	kubectl rollout status deployment/argo-rollouts -n argo-rollouts --timeout=120s
	@echo "==> Applying shared AnalysisTemplate..."
	kubectl apply -f k8s/vllm/analysis-template.yaml
	@echo "==> Replacing Llama3 Deployment with Rollout (canary strategy)..."
	kubectl delete deployment/vllm-llama3 -n llm-serving --ignore-not-found
	kubectl apply -f k8s/vllm/llama3/rollout.yaml
	kubectl apply -f k8s/vllm/llama3/service.yaml
	@echo "==> Argo Rollouts ready."
	@echo "    Promote:  kubectl argo rollouts promote vllm-llama3 -n llm-serving"
	@echo "    Abort:    kubectl argo rollouts abort   vllm-llama3 -n llm-serving"
	@echo "    Status:   kubectl argo rollouts get rollout vllm-llama3 -n llm-serving --watch"
	@echo "    Dashboard: https://rollouts.$(DNS_ZONE)"

# ── WAF + ALB ──────────────────────────────────────────────────────────────────
# AWS WAF v2 WebACL (managed rules + rate limiting) attached to an ALB.
# ALB sits in front of Traefik: Internet → ALB(WAF) → Traefik → backends.
deploy-waf:
	@echo "==> Reading WAF and ALB config from Terraform outputs..."
	$(eval WAF_ACL_ARN          := $(shell cd provision/eks && terraform output -raw waf_acl_arn 2>/dev/null))
	$(eval ALB_CONTROLLER_ROLE  := $(shell cd provision/eks && terraform output -raw alb_controller_role_arn 2>/dev/null))
	@echo "==> Installing AWS Load Balancer Controller..."
	helm repo add eks https://aws.github.io/eks-charts
	helm repo update
	helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
		--namespace kube-system \
		--values helm/aws-load-balancer-controller-values.yaml \
		--set "clusterName=$(CLUSTER_NAME)" \
		--set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$(ALB_CONTROLLER_ROLE)"
	kubectl rollout status deployment/aws-load-balancer-controller -n kube-system --timeout=120s
	@echo "==> Applying ALB Ingress with WAF annotation..."
	sed "s|\$${WAF_ACL_ARN}|$(WAF_ACL_ARN)|g" \
		k8s/base/alb-ingress.yaml | kubectl apply -f -
	@echo "==> WAF active. ALB provisioning takes ~2 min. Check: kubectl get ingress -n traefik"

# ── Operations ─────────────────────────────────────────────────────────────────

health-check:
	@LITELLM_URL="https://vllm.$(DNS_ZONE)" bash scripts/health-check.sh

# FIX #9: No ignore_changes footgun. Updates are explicit kubectl rollout commands.
deploy-vllm-70b:
	@echo "==> Applying multi-GPU NodePool (g5.12xlarge, 4x A10G)..."
	kubectl apply -f k8s/karpenter/node-pool-multigpu.yaml
	@echo "==> Deploying Llama 3.1 70B on vLLM (tensor-parallel-size=4)..."
	kubectl apply -f k8s/vllm/llama3-70b/
	@echo "==> Waiting for 70B pod to pass startupProbe (up to 25 min)..."
	kubectl rollout status deployment/vllm-llama3-70b -n llm-serving --timeout=1500s

redeploy-vllm:
	kubectl rollout restart deployment/vllm-llama3 -n llm-serving
	kubectl rollout restart deployment/vllm-qwen3  -n llm-serving

redeploy-sglang:
	kubectl rollout restart deployment/sglang-llama3 -n llm-serving
	kubectl rollout restart deployment/sglang-qwen3  -n llm-serving

# ── Canary / Progressive Rollout ───────────────────────────────────────────────
# Shifts LiteLLM weighted routing between vLLM and SGLang without redeploying pods.
# Default traffic split is 50/50 (weight=1 each in k8s/litellm/config.yaml).
#
# Examples:
#   make canary ENGINE=vllm WEIGHT=90     # send 90% to vLLM, 10% to SGLang
#   make canary ENGINE=sglang WEIGHT=75   # send 75% to SGLang, 25% to vLLM
#   make promote ENGINE=vllm              # send 100% to vLLM
#   make rollback                         # reset to 50/50

ENGINE ?= vllm
WEIGHT ?= 90

canary:
	@ENGINE=$(ENGINE) WEIGHT=$(WEIGHT) bash scripts/canary.sh canary --engine $(ENGINE) --weight $(WEIGHT)

promote:
	@ENGINE=$(ENGINE) bash scripts/canary.sh promote --engine $(ENGINE)

rollback:
	@bash scripts/canary.sh rollback

# ── Benchmarks (lab2 integration) ──────────────────────────────────────────────

benchmark-prod:
	@echo "==> Running production benchmark (all engines/models)..."
	VLLM_LLAMA3_URL="https://vllm.$(DNS_ZONE)" \
	SGLANG_LLAMA3_URL="https://vllm.$(DNS_ZONE)" \
	LITELLM_MASTER_KEY="$(LITELLM_MASTER_KEY)" \
	python benchmark/production_runner.py --compare

benchmark-compare:
	@echo "==> Comparing lab2 vs production..."
	python benchmark/compare.py \
		--lab $(LAB_RESULTS) \
		--prod $(PROD_RESULTS)

# ── Teardown ───────────────────────────────────────────────────────────────────

teardown:
	@echo "WARNING: This will destroy all cluster resources."
	@read -p "Type 'destroy' to confirm: " CONFIRM; \
	[ "$$CONFIRM" = "destroy" ] || { echo "Aborted."; exit 1; }
	@if [ -d provision/runpod/.terraform ]; then \
		cd provision/runpod && terraform destroy; \
	fi
	@if [ -f provision/vastai/state.json ]; then \
		cd provision/vastai && python3 vastai.py destroy; \
	fi
