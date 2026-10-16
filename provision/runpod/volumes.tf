# RunPod Network Volumes — persistent storage for model weights.
# Each GPU node mounts this via the RunPod CSI driver.
# Models are downloaded once by the init container and cached here.
resource "runpod_network_volume" "model_cache" {
  name        = "${var.project}-model-cache"
  size        = 100  # GB — enough for Llama 3.1 8B AWQ (~10 GB) + Qwen3 8B fp8 (~8 GB) with headroom
  datacenter  = "US-TX-3"
  tags        = local.common_tags
}

# StorageClass backed by the RunPod network volume for PVC binding in k8s manifests.
resource "kubernetes_storage_class" "model_cache" {
  metadata {
    name = "runpod-model-cache"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "false"
    }
  }

  storage_provisioner    = "csi.runpod.io"
  reclaim_policy         = "Retain"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = false

  parameters = {
    "csi.runpod.io/network-volume-id" = runpod_network_volume.model_cache.id
  }
}
