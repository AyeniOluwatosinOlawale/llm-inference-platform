#!/bin/bash
# k3s worker bootstrap for RunPod privileged containers.
# __K3S_URL__ and __K3S_TOKEN__ are replaced by runpod.py at runtime.

set -euo pipefail
exec >> /var/log/k3s-bootstrap.log 2>&1

echo "==> [$(date)] Starting k3s agent bootstrap..."

mkdir -p /root/.ssh
echo "__SSH_PUBLIC_KEY__" >> /root/.ssh/authorized_keys
sort -u /root/.ssh/authorized_keys > /tmp/ak_tmp && mv /tmp/ak_tmp /root/.ssh/authorized_keys
chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
echo "==> SSH key injected"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends curl ca-certificates

modprobe overlay 2>/dev/null || true
modprobe br_netfilter 2>/dev/null || true

[ -e /dev/kmsg ] || ln -sf /dev/null /dev/kmsg

mkdir -p /var/lib/kubelet
mount --bind /var/lib/kubelet /var/lib/kubelet 2>/dev/null || true
mount --make-rshared /var/lib/kubelet 2>/dev/null || true

# NVIDIA container toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq
apt-get install -y --no-install-recommends nvidia-container-toolkit
echo "==> NVIDIA container toolkit installed"

# Install k3s agent (skip systemd start)
export INSTALL_K3S_SKIP_START=true
export K3S_URL=__K3S_URL__
export K3S_TOKEN=__K3S_TOKEN__
curl -sfL https://get.k3s.io | sh -s - agent

nvidia-ctk runtime configure --runtime=containerd 2>/dev/null || true

# Start k3s agent directly
echo "==> Starting k3s agent..."
nohup k3s agent \
    --server __K3S_URL__ \
    --token __K3S_TOKEN__ \
    --snapshotter=native \
    >> /var/log/k3s.log 2>&1 &
echo "k3s agent PID: $!"

echo "==> [$(date)] k3s agent started, joining cluster..."
