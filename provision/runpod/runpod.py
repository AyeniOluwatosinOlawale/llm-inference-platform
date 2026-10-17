"""
RunPod cluster lifecycle management — kubeadm (full Kubernetes 1.30).

Usage:
    python runpod.py create    # provision 2x RTX 4090 VMs, bootstrap kubeadm
    python runpod.py destroy   # terminate all pods
    python runpod.py status    # show pods and kubectl get nodes
    python runpod.py kubeconfig # fetch and print kubeconfig
"""

import os
import sys
import json
import time
import subprocess
import argparse
from pathlib import Path

import requests

API_KEY = os.environ.get("RUNPOD_API_KEY")
if not API_KEY:
    print("Error: RUNPOD_API_KEY environment variable not set")
    sys.exit(1)

GRAPHQL_URL = "https://api.runpod.io/graphql"
STATE_FILE  = Path(__file__).parent / "state.json"
INIT_DIR    = Path(__file__).parent / "cloud-init"
SSH_KEY     = Path.home() / ".ssh" / "id_vastai"
SSH_PUB_KEY = (Path.home() / ".ssh" / "id_vastai.pub").read_text().strip()

GPU_TYPE     = "NVIDIA GeForce RTX 4090"
POD_IMAGE    = "runpod/base:1.4.1-rc.175-ubuntu2204"
DISK_GB      = 100
VCPU         = 8
RAM_GB       = 32


# ── GraphQL helpers ────────────────────────────────────────────────────────

def gql(query: str, variables: dict = None) -> dict:
    resp = requests.post(
        GRAPHQL_URL,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


# ── State ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"server": None, "workers": [], "join_command": None, "server_ip": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Pod lifecycle ──────────────────────────────────────────────────────────

def create_pod(name: str) -> str:
    """Create an on-demand RTX 4090 pod. Returns pod ID."""
    mutation = """
    mutation CreatePod($input: PodFindAndDeployOnDemandInput!) {
      podFindAndDeployOnDemand(input: $input) { id name }
    }
    """
    variables = {
        "input": {
            "name": name,
            "imageName": POD_IMAGE,
            "gpuTypeId": GPU_TYPE,
            "cloudType": "SECURE",
            "supportPublicIp": True,
            "startSsh": True,
            "containerDiskInGb": DISK_GB,
            "minVcpuCount": VCPU,
            "minMemoryInGb": RAM_GB,
            "gpuCount": 1,
            "ports": "22/tcp,6443/tcp,8080/tcp",
            "env": [
                {"key": "PUBLIC_KEY", "value": SSH_PUB_KEY},
                {"key": "SSH_PUBLIC_KEY", "value": SSH_PUB_KEY},
            ],
        }
    }
    result = gql(mutation, variables)
    pod_id = result["podFindAndDeployOnDemand"]["id"]
    print(f"  Created pod {pod_id} ({name})")
    return pod_id


def get_pod(pod_id: str) -> dict:
    query = """
    query GetPod($podId: String!) {
      pod(input: { podId: $podId }) {
        id name desiredStatus
        runtime {
          ports { ip isIpPublic privatePort publicPort type }
        }
      }
    }
    """
    return gql(query, {"podId": pod_id})["pod"]


def wait_for_ssh(pod_id: str, timeout: int = 600) -> tuple:
    """Wait until pod is RUNNING with SSH port. Returns (host, port)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pod = get_pod(pod_id)
        status = pod.get("desiredStatus", "?")
        ports  = (pod.get("runtime") or {}).get("ports") or []
        ssh    = next((p for p in ports if p.get("privatePort") == 22 and p.get("isIpPublic")), None)
        if status == "RUNNING" and ssh:
            return ssh["ip"], int(ssh["publicPort"])
        print(f"  [{pod_id}] status={status} ssh={'yes' if ssh else 'no'}")
        time.sleep(15)
    raise TimeoutError(f"Pod {pod_id} not ready after {timeout}s")


def terminate_pod(pod_id: str) -> None:
    gql('mutation T($id: String!) { podTerminate(input:{podId:$id}) }', {"id": pod_id})
    print(f"  Terminated {pod_id}")


# ── SSH helpers ────────────────────────────────────────────────────────────

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=15",
    "-o", "IdentitiesOnly=yes",
    "-i", str(SSH_KEY),
]

def ssh(host: str, port: int, cmd: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["ssh"] + SSH_OPTS + ["-p", str(port), f"root@{host}", cmd],
        capture_output=True, text=True, timeout=timeout + 15,
    )
    return result.stdout.strip()


def ssh_copy(host: str, port: int, local: Path, remote: str) -> None:
    subprocess.run(
        ["scp"] + SSH_OPTS + ["-P", str(port), str(local), f"root@{host}:{remote}"],
        check=True, timeout=60,
    )


def wait_for_ssh_ready(host: str, port: int, retries: int = 20) -> None:
    for i in range(retries):
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + ["-p", str(port), f"root@{host}", "echo ready"],
            capture_output=True, text=True, timeout=20,
        )
        if "ready" in result.stdout:
            return
        print(f"  SSH not ready yet ({i+1}/{retries})...")
        time.sleep(15)
    raise TimeoutError("SSH never became ready")


# ── Bootstrap helpers ──────────────────────────────────────────────────────

def run_bootstrap(host: str, port: int, script_text: str) -> None:
    """Upload and run a bootstrap script on the pod."""
    # Write script to a temp file and scp it
    tmp = Path("/tmp/k8s-bootstrap.sh")
    tmp.write_text(script_text)
    ssh_copy(host, port, tmp, "/tmp/bootstrap.sh")
    ssh(host, port, "chmod +x /tmp/bootstrap.sh && nohup bash /tmp/bootstrap.sh > /var/log/k8s-bootstrap.log 2>&1 &", timeout=30)


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_create(args) -> None:
    state = load_state()

    # ── Control plane ────────────────────────────────────────────────────────
    print("==> Creating control plane pod (RTX 4090)...")
    cp_id = create_pod("k8s-control-plane")
    state["server"] = {"id": cp_id}
    save_state(state)

    print(f"==> Waiting for control plane {cp_id}...")
    cp_host, cp_port = wait_for_ssh(cp_id)
    state["server"].update({"host": cp_host, "port": cp_port})
    save_state(state)
    print(f"  SSH ready: {cp_host}:{cp_port}")

    wait_for_ssh_ready(cp_host, cp_port)

    # Prepare and run control plane bootstrap (k3s, no systemd needed)
    server_script = (INIT_DIR / "server.sh").read_text()
    server_script = server_script.replace("__SSH_PUBLIC_KEY__", SSH_PUB_KEY)
    print("==> Running k3s control plane bootstrap (~10 min)...")
    run_bootstrap(cp_host, cp_port, server_script)

    # Poll until k3s node-token appears
    print("==> Polling for k3s completion...")
    k3s_token = ""
    for attempt in range(40):
        out = ssh(cp_host, cp_port, "cat /var/lib/rancher/k3s/server/node-token 2>/dev/null", timeout=20)
        if out and len(out) > 10:
            k3s_token = out.strip()
            break
        print(f"  Not ready yet ({attempt+1}/40), waiting 30s...")
        time.sleep(30)

    if not k3s_token:
        log = ssh(cp_host, cp_port, "tail -30 /var/log/k3s-bootstrap.log", timeout=20)
        raise RuntimeError(f"k3s server never completed.\nLog:\n{log}")

    server_ip = ssh(cp_host, cp_port, "curl -s ifconfig.me 2>/dev/null || curl -s api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'", timeout=15)
    state["k3s_token"] = k3s_token
    state["server_ip"] = server_ip
    save_state(state)
    print(f"  Control plane ready! Server IP: {server_ip}")

    # ── Worker node ──────────────────────────────────────────────────────────
    print("==> Creating worker pod (RTX 4090)...")
    wk_id = create_pod("k3s-worker-0")
    state["workers"].append({"id": wk_id})
    save_state(state)

    print(f"==> Waiting for worker {wk_id}...")
    wk_host, wk_port = wait_for_ssh(wk_id)
    state["workers"][0].update({"host": wk_host, "port": wk_port})
    save_state(state)
    wait_for_ssh_ready(wk_host, wk_port)

    agent_script = (INIT_DIR / "agent.sh").read_text()
    agent_script = agent_script.replace("__SSH_PUBLIC_KEY__", SSH_PUB_KEY)
    agent_script = agent_script.replace("__K3S_URL__", f"https://{server_ip}:6443")
    agent_script = agent_script.replace("__K3S_TOKEN__", k3s_token)
    print("==> Running k3s worker bootstrap...")
    run_bootstrap(wk_host, wk_port, agent_script)

    print("\n==> Cluster provisioned!")
    print(f"\n  Fetch kubeconfig:")
    print(f"    ssh -i ~/.ssh/id_vastai -p {cp_port} root@{cp_host} cat /etc/rancher/k3s/k3s.yaml > kubeconfig")
    print(f"    sed -i '' 's/127.0.0.1/{server_ip}/' kubeconfig")
    print(f"    export KUBECONFIG=$(pwd)/kubeconfig")
    print(f"    kubectl get nodes")


def cmd_destroy(args) -> None:
    state = load_state()
    ids = []
    if state.get("server"):
        ids.append(state["server"]["id"])
    for w in state.get("workers", []):
        ids.append(w["id"])
    for pod_id in ids:
        print(f"==> Terminating {pod_id}...")
        try:
            terminate_pod(pod_id)
        except Exception as e:
            print(f"  Warning: {e}")
    STATE_FILE.unlink(missing_ok=True)
    print("==> Done.")


def cmd_status(args) -> None:
    state = load_state()
    if not state.get("server"):
        print("No state. Run 'create' first.")
        return
    srv = state["server"]
    print(f"Control plane: {srv['id']}  {srv.get('host')}:{srv.get('port')}")
    for i, w in enumerate(state.get("workers", [])):
        print(f"Worker {i}: {w['id']}  {w.get('host')}:{w.get('port')}")
    if state.get("server_ip"):
        srv = state["server"]
        print(f"\nkubectl:")
        print(f"  ssh -i ~/.ssh/id_vastai -p {srv.get('port')} root@{srv.get('host')} kubectl get nodes")


def cmd_kubeconfig(args) -> None:
    state = load_state()
    if not state.get("server"):
        print("No state. Run 'create' first.")
        return
    srv = state["server"]
    host, port = srv["host"], srv["port"]
    ip = state.get("server_ip", host)
    kubeconfig = ssh(host, port, "cat /etc/rancher/k3s/k3s.yaml", timeout=15)
    kubeconfig = kubeconfig.replace("127.0.0.1", ip)
    out = Path("kubeconfig")
    out.write_text(kubeconfig)
    print(f"Kubeconfig saved to ./kubeconfig")
    print(f"  export KUBECONFIG=$(pwd)/kubeconfig")
    print(f"  kubectl get nodes")


def main():
    parser = argparse.ArgumentParser(description="RunPod kubeadm cluster lifecycle")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("create")
    sub.add_parser("destroy")
    sub.add_parser("status")
    sub.add_parser("kubeconfig")
    args = parser.parse_args()
    cmds = {"create": cmd_create, "destroy": cmd_destroy,
            "status": cmd_status, "kubeconfig": cmd_kubeconfig}
    if args.command in cmds:
        cmds[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
