# ── GKE Cluster ───────────────────────────────────────────────────────────────

resource "google_container_cluster" "main" {
  name     = var.cluster_name
  location = var.region

  network    = google_compute_network.main.id
  subnetwork = google_compute_subnetwork.main.id

  # Remove default node pool — we manage node pools separately
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  release_channel {
    channel = "STABLE"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  addons_config {
    horizontal_pod_autoscaling {
      disabled = false
    }
    http_load_balancing {
      disabled = false
    }
  }

  # Enable cluster autoscaler via node pool config
  cluster_autoscaling {
    enabled = false # We use per-pool autoscaling below
  }
}

# ── CPU node pool ─────────────────────────────────────────────────────────────
# Runs: LiteLLM, Redis, Postgres, KEDA, Prometheus, Grafana, Loki, Traefik

resource "google_container_node_pool" "cpu" {
  name     = "cpu-nodes"
  cluster  = google_container_cluster.main.id
  location = var.region

  autoscaling {
    min_node_count = var.cpu_node_count
    max_node_count = 4
  }

  node_config {
    machine_type = var.cpu_machine_type
    disk_size_gb = var.cpu_disk_size_gb
    disk_type    = "pd-ssd"
    image_type   = "COS_CONTAINERD"

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    labels = {
      workload-type = "cpu"
    }

    shielded_instance_config {
      enable_secure_boot = true
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}

# ── GPU node pool ─────────────────────────────────────────────────────────────
# NVIDIA L4 24GB Spot — confirmed available in us-central1-a

resource "google_container_node_pool" "gpu" {
  name           = "gpu-nodes"
  cluster        = google_container_cluster.main.id
  location       = var.region
  node_locations = ["us-central1-a", "us-central1-b", "us-central1-c"] # All zones for Spot capacity
  autoscaling {
    min_node_count = var.gpu_node_count
    max_node_count = 1
  }

  node_config {
    machine_type = var.gpu_machine_type
    disk_size_gb = var.gpu_disk_size_gb
    disk_type    = "pd-standard"
    image_type   = "COS_CONTAINERD"
    spot         = false # On-demand — no preemption, us-central1 L4 confirmed available

    guest_accelerator {
      type  = var.gpu_accelerator_type
      count = 1
      gpu_driver_installation_config {
        gpu_driver_version = "LATEST"
      }
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]

    labels = {
      workload-type    = "gpu"
      accelerator-type = "l4"
    }

    taint {
      key    = "nvidia.com/gpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    shielded_instance_config {
      enable_secure_boot = true
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}
