#!/bin/bash
# k3s GPU worker agent bootstrap (ubuntu:22.04 base image).
# __K3S_URL__ and __K3S_TOKEN__ are replaced by provision/vastai/vastai.py at runtime.

mkdir -p /var/log 2>/dev/null || true
exec >> /var/log/k3s-bootstrap.log 2>&1 || true

echo "==> [$(date)] Starting k3s agent bootstrap..."

# Inject SSH public key
mkdir -p /root/.ssh
echo "__SSH_PUBLIC_KEY__" >> /root/.ssh/authorized_keys
sort -u /root/.ssh/authorized_keys > /tmp/ak_tmp && mv /tmp/ak_tmp /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
echo "==> SSH public key injected"

# Install dependencies
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends curl open-iscsi ca-certificates gnupg

# Install NVIDIA container toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq
apt-get install -y --no-install-recommends nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=containerd || true

# Join k3s cluster as agent
K3S_URL=__K3S_URL__
K3S_TOKEN=__K3S_TOKEN__

echo "==> Joining k3s cluster at $K3S_URL..."
export INSTALL_K3S_SKIP_START=true
curl -sfL https://get.k3s.io | K3S_URL=$K3S_URL K3S_TOKEN=$K3S_TOKEN sh -s - agent \
    --node-label "role=gpu-serving" \
    --node-label "nvidia.com/gpu.present=true"

# Start k3s agent directly (no systemd in Vast.ai containers)
nohup k3s agent \
    --server "$K3S_URL" \
    --token "$K3S_TOKEN" \
    --node-label "role=gpu-serving" \
    --node-label "nvidia.com/gpu.present=true" \
    --snapshotter=native \
    >> /var/log/k3s-agent.log 2>&1 &
echo "k3s agent started with PID $!"

echo "==> [$(date)] k3s GPU agent joined cluster"
