#!/bin/bash
# k3s control plane bootstrap for Vast.ai containers (no systemd — containers use bash as PID 1).

mkdir -p /var/log 2>/dev/null || true
exec >> /var/log/k3s-bootstrap.log 2>&1 || true

echo "==> [$(date)] Starting k3s server bootstrap..."

# Inject SSH public key
mkdir -p /root/.ssh
echo "__SSH_PUBLIC_KEY__" >> /root/.ssh/authorized_keys
sort -u /root/.ssh/authorized_keys > /tmp/ak_tmp && mv /tmp/ak_tmp /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
echo "==> SSH public key injected"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends curl jq open-iscsi ca-certificates gnupg

# Install NVIDIA container toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq
apt-get install -y --no-install-recommends nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=containerd || true

# Install k3s binary only (INSTALL_K3S_SKIP_START=true avoids the failed systemctl start)
PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s api.ipify.org 2>/dev/null || echo "127.0.0.1")
export INSTALL_K3S_SKIP_START=true
curl -sfL https://get.k3s.io | sh -s - server \
    --secrets-encryption \
    --disable traefik \
    --disable servicelb \
    --write-kubeconfig-mode 644 \
    --tls-san "$PUBLIC_IP"

# Fix /dev/kmsg missing (kubelet requires it)
ln -sf /dev/null /dev/kmsg 2>/dev/null || true

# Pre-mount kubelet root dir as shared (requires CAP_SYS_ADMIN)
mkdir -p /var/lib/kubelet
mount --bind /var/lib/kubelet /var/lib/kubelet 2>/dev/null || true
mount --make-rshared /var/lib/kubelet 2>/dev/null || true

# Start k3s directly (no systemd in Vast.ai containers)
echo "==> Starting k3s server directly (no systemd)..."
nohup k3s server \
    --secrets-encryption \
    --disable traefik \
    --disable servicelb \
    --write-kubeconfig-mode 644 \
    --tls-san "$PUBLIC_IP" \
    --snapshotter=native \
    >> /var/log/k3s.log 2>&1 &
echo "k3s server started with PID $!"

# Wait for k3s API to be ready
echo "==> Waiting for k3s to become ready..."
for i in $(seq 1 60); do
    if kubectl get nodes 2>/dev/null | grep -q "Ready"; then
        echo "==> k3s is ready!"
        break
    fi
    echo "  attempt $i/60..."
    sleep 10
done

# Install NVIDIA device plugin
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.16.2/deployments/static/nvidia-device-plugin.yml || true

k3s secrets-encrypt rotate || true

echo "==> [$(date)] k3s server ready. Node token: $(cat /var/lib/rancher/k3s/server/node-token)"
