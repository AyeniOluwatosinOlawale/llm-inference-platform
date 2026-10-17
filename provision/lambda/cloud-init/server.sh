#!/bin/bash
# kubeadm control plane bootstrap for Lambda Labs A10 VMs (Ubuntu 22.04, full kernel access).
# Runs as root via cloud-init user_data.

set -euo pipefail
exec >> /var/log/k8s-bootstrap.log 2>&1

echo "==> [$(date)] Starting kubeadm control plane bootstrap..."

# Inject SSH public key for ubuntu user
mkdir -p /home/ubuntu/.ssh
echo "__SSH_PUBLIC_KEY__" >> /home/ubuntu/.ssh/authorized_keys
sort -u /home/ubuntu/.ssh/authorized_keys > /tmp/ak_tmp && mv /tmp/ak_tmp /home/ubuntu/.ssh/authorized_keys
chmod 700 /home/ubuntu/.ssh && chmod 600 /home/ubuntu/.ssh/authorized_keys
chown -R ubuntu:ubuntu /home/ubuntu/.ssh
echo "==> SSH key injected"

export DEBIAN_FRONTEND=noninteractive

# ── Kernel modules & sysctl ─────────────────────────────────────────────────
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter

cat > /etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system

# ── containerd ──────────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y --no-install-recommends curl ca-certificates gnupg apt-transport-https

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y containerd.io

mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd
systemctl enable containerd
echo "==> containerd installed"

# ── NVIDIA container toolkit ─────────────────────────────────────────────────
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=containerd
systemctl restart containerd
echo "==> NVIDIA container toolkit installed"

# ── kubeadm / kubelet / kubectl ─────────────────────────────────────────────
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key \
  | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] \
  https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /" \
  > /etc/apt/sources.list.d/kubernetes.list

apt-get update -qq
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl
systemctl enable kubelet
echo "==> kubeadm + kubelet + kubectl installed"

# ── kubeadm init ─────────────────────────────────────────────────────────────
PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
PRIVATE_IP=$(hostname -I | awk '{print $1}')
echo "==> Public IP: $PUBLIC_IP  Private IP: $PRIVATE_IP"

kubeadm init \
  --apiserver-advertise-address="$PRIVATE_IP" \
  --apiserver-cert-extra-sans="$PUBLIC_IP" \
  --pod-network-cidr=10.244.0.0/16 \
  --node-name="$(hostname)"

# kubeconfig for root and ubuntu
mkdir -p /root/.kube
cp /etc/kubernetes/admin.conf /root/.kube/config
chmod 600 /root/.kube/config

mkdir -p /home/ubuntu/.kube
cp /etc/kubernetes/admin.conf /home/ubuntu/.kube/config
chown ubuntu:ubuntu /home/ubuntu/.kube/config
echo "==> kubeconfig written"

# ── Flannel CNI ──────────────────────────────────────────────────────────────
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
echo "==> Flannel CNI installed"

# ── NVIDIA device plugin ─────────────────────────────────────────────────────
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.16.2/deployments/static/nvidia-device-plugin.yml
echo "==> NVIDIA device plugin installed"

# ── Save join command ────────────────────────────────────────────────────────
kubeadm token create --print-join-command > /root/kubeadm-join.sh
chmod 600 /root/kubeadm-join.sh
echo "==> Join command saved"

echo "==> [$(date)] kubeadm control plane ready!"
kubectl get nodes
