# Runbook: GPU Node Failure

## Symptoms
- `VllmPodNotReady` or `SGLangPodNotReady` alert fires
- `kubectl get pods -n llm-serving` shows pods in `Pending` or `CrashLoopBackOff`
- LiteLLM returns 503 for the affected model

## Diagnosis
```bash
kubectl get nodes -l role=gpu-serving
kubectl describe pod <failing-pod> -n llm-serving
kubectl logs <failing-pod> -n llm-serving --previous
```

## Recovery

### RunPod
KEDA will fire a scale-up event. If the node pool is exhausted, RunPod K8s auto-provisions a new node. Wait 3-5 minutes and check:
```bash
kubectl get nodes --watch
kubectl get pods -n llm-serving --watch
```

### Vast.ai
```bash
cd provision/vastai
python vastai.py add-gpu    # provisions one additional GPU worker
python vastai.py status     # confirm new node joined k3s
```
Then confirm the pod is rescheduled:
```bash
kubectl get pods -n llm-serving -w
```

## Prevention
- `topologySpreadConstraints` ensures replicas land on different nodes.
- `minReplicaCount: 1` means KEDA always keeps one warm replica.
- Increase `minReplicaCount: 2` in scaled-object.yaml for zero-downtime resilience.
