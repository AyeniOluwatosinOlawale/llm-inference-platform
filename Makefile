# llm-inference-platform — provider-agnostic LLM serving on RunPod / Vast.ai
# Engines: vLLM v0.30.0 + SGLang v0.5.20  |  Models: Llama 3.1 8B + Qwen3 8B
# Lab integration: inference-optimizer-lab2 (VLLMClient / SGLangClient)

.PHONY: help secrets up-runpod up-vastai \
        deploy-base deploy-infra verify-metrics \
        deploy-vllm deploy-sglang deploy-litellm deploy-dashboards \
        health-check load-test \
        benchmark-lab benchmark-prod benchmark-compare \
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
	@echo "    make deploy-base    Namespaces + NetworkPolicies"
	@echo "    make deploy-infra   Prometheus, KEDA, Traefik, Loki"
	@echo "    make verify-metrics Confirm vLLM/SGLang metric labels"
	@echo "    make deploy-vllm    Llama3 + Qwen3 on vLLM v0.30.0"
	@echo "    make deploy-sglang  Llama3 + Qwen3 on SGLang v0.5.20"
	@echo "    make deploy-litellm LiteLLM gateway"
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

up-runpod:
	@echo "==> Provisioning RunPod K8s cluster..."
	cd provision/runpod && terraform init && terraform apply -var="dns_zone=$(DNS_ZONE)"
	@echo "==> Fetching kubeconfig..."
	cd provision/runpod && terraform output -raw kubeconfig > ../../kubeconfig
	@echo "    export KUBECONFIG=$(PWD)/kubeconfig"

up-vastai:
	@echo "==> Provisioning Vast.ai instances + k3s..."
	cd provision/vastai && python vastai.py create
	@echo "    Follow the kubeconfig instructions printed above."

# ── Deploy ─────────────────────────────────────────────────────────────────────

deploy-base:
	kubectl apply -f k8s/base/namespaces.yaml
	kubectl apply -f k8s/base/network-policies/

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
		--set grafana.adminPassword="$(GRAFANA_ADMIN_PASSWORD)"
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
	@echo "==> Injecting LiteLLM secrets..."
	kubectl create secret generic litellm-secrets -n llm-gateway \
		--from-literal=LITELLM_MASTER_KEY="$(LITELLM_MASTER_KEY)" \
		--from-literal=REDIS_PASSWORD="$(REDIS_PASSWORD)" \
		--from-literal=POSTGRES_PASSWORD="$(POSTGRES_PASSWORD)" \
		--from-literal=DATABASE_URL="postgresql://litellm:$(POSTGRES_PASSWORD)@postgres-service.llm-gateway.svc.cluster.local:5432/litellm" \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f k8s/litellm/postgres.yaml
	kubectl apply -f k8s/litellm/redis.yaml
	kubectl apply -f k8s/litellm/config.yaml
	kubectl apply -f k8s/litellm/deployment.yaml
	kubectl apply -f k8s/litellm/hpa.yaml
	kubectl apply -f k8s/litellm/pdb.yaml
	kubectl apply -f k8s/litellm/ingress.yaml
	kubectl rollout status deployment/litellm-proxy -n llm-gateway --timeout=120s

deploy-dashboards:
	kubectl apply -f k8s/monitoring/dashboards/dashboards-configmap.yaml

# ── Operations ─────────────────────────────────────────────────────────────────

health-check:
	@LITELLM_URL="https://vllm.$(DNS_ZONE)" bash scripts/health-check.sh

# FIX #9: No ignore_changes footgun. Updates are explicit kubectl rollout commands.
redeploy-vllm:
	kubectl rollout restart deployment/vllm-llama3 -n llm-serving
	kubectl rollout restart deployment/vllm-qwen3  -n llm-serving

redeploy-sglang:
	kubectl rollout restart deployment/sglang-llama3 -n llm-serving
	kubectl rollout restart deployment/sglang-qwen3  -n llm-serving

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
		cd provision/vastai && python vastai.py destroy; \
	fi
