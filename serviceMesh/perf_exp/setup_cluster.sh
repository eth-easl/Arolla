#!/bin/bash
set -e

MASTER_NODE="pc729.emulab.net"
WORKER_NODE="pc735.emulab.net"
USER="yazhuoz"

# Function to run command on node
run_on() {
    local node=$1
    local cmd=$2
    echo "[$node] Executing: $cmd"
    ssh -o StrictHostKeyChecking=no ${USER}@${node} "$cmd"
}

# 1. Install Dependencies (Docker, Kubeadm, Kubelet, Kubectl) on BOTH nodes
setup_node() {
    local node=$1
    echo "=== Setting up prerequisites on $node ==="
    
    # 1. Cleaning up conflicting packages AND apt sources
    run_on $node "sudo apt-get remove -y docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc || true"
    run_on $node "sudo rm -f /etc/apt/sources.list.d/docker.list"
    run_on $node "sudo rm -f /etc/apt/sources.list.d/docker.sources"
    
    # 2. Add Docker Official Repo (Fixes conflicts and duplicates)
    run_on $node "sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg apt-transport-https"
    run_on $node "sudo install -m 0755 -d /etc/apt/keyrings"
    run_on $node "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes"
    run_on $node "sudo chmod a+r /etc/apt/keyrings/docker.gpg"
    # Overwrite docker.list to ensure no duplicates/conflicts
    run_on $node "echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \$(. /etc/os-release && echo \"\$VERSION_CODENAME\") stable\" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null"
    
    # 3. Install Docker CE and Containerd
    run_on $node "sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io"
    
    # 4. Configure Containerd for Kubernetes (SystemdCgroup)
    run_on $node "sudo mkdir -p /etc/containerd"
    run_on $node "containerd config default | sudo tee /etc/containerd/config.toml > /dev/null"
    run_on $node "sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml"
    run_on $node "sudo systemctl restart containerd"

    # 5. Install Kubernetes Components
    run_on $node "curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg --yes"
    run_on $node "echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list"
    run_on $node "sudo apt-get update && sudo apt-get install -y kubelet kubeadm kubectl"
    run_on $node "sudo apt-mark hold kubelet kubeadm kubectl"
    
    # 6. Disable swap
    run_on $node "sudo swapoff -a && sudo sed -i '/ swap / s/^\(.*\)$/#\1/g' /etc/fstab"
    
    # 6.5 Disable Firewall (per article recommendation)
    run_on $node "sudo systemctl stop ufw && sudo systemctl disable ufw || true"
    
    # 7. Load modules and sysctl
    run_on $node "sudo modprobe overlay && sudo modprobe br_netfilter"
    run_on $node "cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF"
    run_on $node "sudo sysctl --system"
    
    # 8. Enable Kubelet
    run_on $node "sudo systemctl enable --now kubelet"
}

setup_node $MASTER_NODE
setup_node $WORKER_NODE

# 1.5 Clean and Reset Nodes (Ensure fresh state)
echo "=== Resetting Nodes to clean state ==="
run_on $MASTER_NODE "sudo kubeadm reset -f || true"
run_on $WORKER_NODE "sudo kubeadm reset -f || true"
run_on $MASTER_NODE "sudo rm -rf /etc/cni/net.d \$HOME/.kube"
run_on $WORKER_NODE "sudo rm -rf /etc/cni/net.d \$HOME/.kube"

# 1.6 Restart Containerd and Wait (Ensure CRI is up)
echo "=== Restarting Containerd and waiting for socket ==="
run_on $MASTER_NODE "sudo systemctl restart containerd"
run_on $WORKER_NODE "sudo systemctl restart containerd"
# Wait for socket
run_on $MASTER_NODE "while [ ! -S /var/run/containerd/containerd.sock ]; do echo 'Waiting for containerd...'; sleep 1; done"
run_on $WORKER_NODE "while [ ! -S /var/run/containerd/containerd.sock ]; do echo 'Waiting for containerd...'; sleep 1; done"

# 2. Initialize Master
echo "=== Initializing Master on $MASTER_NODE ==="
# Pod network cidr is required for Flannel
run_on $MASTER_NODE "sudo kubeadm init --pod-network-cidr=10.244.0.0/16 --ignore-preflight-errors=NumCPU"

# 3. Setup Kubeconfig on Master for User
run_on $MASTER_NODE "mkdir -p \$HOME/.kube && sudo cp -f /etc/kubernetes/admin.conf \$HOME/.kube/config && sudo chown \$(id -u):\$(id -g) \$HOME/.kube/config"

# 4. Install CNI (Flannel)
echo "=== Installing Flannel CNI ==="
# Use sudo and explicit config to avoid environment issues
run_on $MASTER_NODE "sudo kubectl --kubeconfig /etc/kubernetes/admin.conf apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml"

# 5. Get Join Command
# Use direct SSH to avoid capturing the "Executing: " echo from run_on
JOIN_CMD=$(ssh -o StrictHostKeyChecking=no ${USER}@${MASTER_NODE} "sudo kubeadm token create --print-join-command")
echo "Join Command: $JOIN_CMD"

# 6. Join Worker
echo "=== Joining Worker $WORKER_NODE ==="
run_on $WORKER_NODE "sudo $JOIN_CMD"

# 7. Configure Local Access
echo "=== Configuring Local kubectl Access ==="
mkdir -p ~/.kube
# Use direct SSH to avoid capturing "Executing: ..." debug log in the file
ssh -o StrictHostKeyChecking=no ${USER}@${MASTER_NODE} "sudo cat /etc/kubernetes/admin.conf" > ~/.kube/config

# Fetch the actual hostname from the node (matches k8s cert)
# Use direct SSH to avoid capturing the "Executing: " echo from run_on
CERT_HOSTNAME=$(ssh -o StrictHostKeyChecking=no ${USER}@${MASTER_NODE} "hostname" | tr -d '[:space:]')
echo "Updating kubeconfig to use hostname: ${CERT_HOSTNAME}"

# Get the current IP/Host in the config
CURRENT_SERVER=$(grep server ~/.kube/config | awk -F "/" '{print $3}' | awk -F ":" '{print $1}')

# Replace with the correct cert hostname
sed -i "s/${CURRENT_SERVER}/${CERT_HOSTNAME}/g" ~/.kube/config

chmod 600 ~/.kube/config

# 8. Install Istio
echo "=== Installing Istio ==="
ssh -o StrictHostKeyChecking=no ${USER}@${MASTER_NODE} "curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.22.1 TARGET_ARCH=x86_64 sh - && cd istio-1.22.1 && sudo ./bin/istioctl install --set profile=demo -y"

# 9. Enable Sidecar Injection
echo "=== Enabling Sidecar Injection ==="
kubectl label namespace default istio-injection=enabled --overwrite

echo "=== Setup Complete. Verification: ==="
kubectl get nodes
