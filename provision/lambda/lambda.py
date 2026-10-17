"""
Lambda Labs VM lifecycle management — kubeadm (full Kubernetes 1.30).

Usage:
    python lambda.py create     # provision 2x A10 VMs, bootstrap kubeadm
    python lambda.py destroy    # terminate all instances
    python lambda.py status     # show instances and kubectl get nodes
    python lambda.py kubeconfig # fetch and save kubeconfig
"""

import os
import sys
import json
import time
import subprocess
import argparse
from pathlib import Path

import requests

API_KEY   = os.environ.get("LAMBDA_API_KEY")
if not API_KEY:
    print("Error: LAMBDA_API_KEY environment variable not set")
    sys.exit(1)

BASE_URL    = "https://cloud.lambdalabs.com/api/v1"
STATE_FILE  = Path(__file__).parent / "state.json"
INIT_DIR    = Path(__file__).parent / "cloud-init"
SSH_KEY     = Path.home() / ".ssh" / "id_vastai"
SSH_KEY_ID  = "e855c52ee4644ede9a179edcbd02341b"   # id-vastai-provisioning

INSTANCE_TYPE = "gpu_1x_a10"
REGION        = "us-east-1"


# ── REST helpers ───────────────────────────────────────────────────────────

def api(method: str, path: str, **kwargs) -> dict:
    resp = requests.request(
        method,
        f"{BASE_URL}{path}",
        auth=(API_KEY, ""),
        timeout=30,
        **kwargs,
    )
    resp.raise_for_status()
    if resp.content:
        return resp.json()
    return {}


# ── State ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"server": None, "workers": [], "join_command": None, "server_ip": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Instance lifecycle ─────────────────────────────────────────────────────

def launch_instance(name: str, user_data: str) -> str:
    """Launch a Lambda A10 VM. Returns instance ID."""
    result = api("POST", "/instance-operations/launch", json={
        "region_name": REGION,
        "instance_type_name": INSTANCE_TYPE,
        "ssh_key_names": ["id-vastai-provisioning"],
        "name": name,
        "user_data": user_data,
    })
    instance_ids = result.get("data", {}).get("instance_ids", [])
    if not instance_ids:
        raise RuntimeError(f"Launch failed: {result}")
    iid = instance_ids[0]
    print(f"  Launched {iid} ({name})")
    return iid


def get_instance(instance_id: str) -> dict:
    return api("GET", f"/instances/{instance_id}").get("data", {})


def wait_for_running(instance_id: str, timeout: int = 300) -> str:
    """Wait until instance is active and return its public IP."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        inst = get_instance(instance_id)
        status = inst.get("status", "?")
        ip = inst.get("ip", "")
        if status == "active" and ip:
            return ip
        print(f"  [{instance_id}] status={status} ip={ip or 'pending'}")
        time.sleep(15)
    raise TimeoutError(f"Instance {instance_id} not active after {timeout}s")


def terminate_instance(instance_id: str) -> None:
    api("POST", "/instances/terminate", json={"instance_ids": [instance_id]})
    print(f"  Terminated {instance_id}")


# ── SSH helpers ────────────────────────────────────────────────────────────

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=15",
    "-o", "IdentitiesOnly=yes",
    "-i", str(SSH_KEY),
]


def ssh(host: str, cmd: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["ssh"] + SSH_OPTS + [f"ubuntu@{host}", cmd],
        capture_output=True, text=True, timeout=timeout + 15,
    )
    return result.stdout.strip()


def ssh_copy(host: str, local: Path, remote: str) -> None:
    subprocess.run(
        ["scp"] + SSH_OPTS + [str(local), f"ubuntu@{host}:{remote}"],
        check=True, timeout=60,
    )


def wait_for_ssh_ready(host: str, retries: int = 30) -> None:
    for i in range(retries):
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"ubuntu@{host}", "echo ready"],
            capture_output=True, text=True, timeout=20,
        )
        if "ready" in result.stdout:
            return
        print(f"  SSH not ready yet ({i+1}/{retries})...")
        time.sleep(15)
    raise TimeoutError("SSH never became ready")


def run_bootstrap(host: str, script_text: str) -> None:
    tmp = Path("/tmp/k8s-bootstrap.sh")
    tmp.write_text(script_text)
    ssh_copy(host, tmp, "/tmp/bootstrap.sh")
    ssh(host, "chmod +x /tmp/bootstrap.sh && nohup sudo bash /tmp/bootstrap.sh > /var/log/k8s-bootstrap.log 2>&1 &", timeout=30)


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_create(args) -> None:
    state = load_state()
    SSH_PUB_KEY = SSH_KEY.with_suffix(".pub").read_text().strip()

    # ── Control plane ────────────────────────────────────────────────────────
    print("==> Launching control plane (A10 24GB)...")
    server_script = (INIT_DIR / "server.sh").read_text()
    server_script = server_script.replace("__SSH_PUBLIC_KEY__", SSH_PUB_KEY)

    cp_id = launch_instance("k8s-control-plane", user_data=server_script)
    state["server"] = {"id": cp_id}
    save_state(state)

    print(f"==> Waiting for control plane {cp_id} to become active...")
    cp_ip = wait_for_running(cp_id)
    state["server"]["ip"] = cp_ip
    save_state(state)
    print(f"  Active: {cp_ip}")

    # Lambda VMs run user_data via cloud-init — wait for SSH
    print("==> Waiting for SSH...")
    wait_for_ssh_ready(cp_ip)

    # Poll until kubeadm init completes
    print("==> Polling for kubeadm completion (~15 min)...")
    join_command = ""
    for attempt in range(50):
        out = ssh(cp_ip, "sudo cat /root/kubeadm-join.sh 2>/dev/null", timeout=20)
        if out and "kubeadm join" in out:
            join_command = out.strip()
            break
        print(f"  Not ready yet ({attempt+1}/50), waiting 30s...")
        time.sleep(30)

    if not join_command:
        log = ssh(cp_ip, "sudo tail -40 /var/log/k8s-bootstrap.log", timeout=20)
        raise RuntimeError(f"kubeadm init never completed.\nLog:\n{log}")

    state["join_command"] = join_command
    state["server_ip"] = cp_ip
    save_state(state)
    print(f"  Control plane ready! IP: {cp_ip}")

    # ── Worker node ──────────────────────────────────────────────────────────
    print("==> Launching worker (A10 24GB)...")
    agent_script = (INIT_DIR / "agent.sh").read_text()
    agent_script = agent_script.replace("__SSH_PUBLIC_KEY__", SSH_PUB_KEY)
    agent_script = agent_script.replace("__JOIN_COMMAND__", join_command)
    agent_script = agent_script.replace("__CP_IP__", cp_ip)

    wk_id = launch_instance("k8s-worker-0", user_data=agent_script)
    state["workers"].append({"id": wk_id})
    save_state(state)

    wk_ip = wait_for_running(wk_id)
    state["workers"][0]["ip"] = wk_ip
    save_state(state)

    print(f"\n==> Cluster provisioned!")
    print(f"\n  Fetch kubeconfig:")
    print(f"    python lambda.py kubeconfig")
    print(f"    export KUBECONFIG=$(pwd)/kubeconfig")
    print(f"    kubectl get nodes")


def cmd_destroy(args) -> None:
    state = load_state()
    ids = []
    if state.get("server"):
        ids.append(state["server"]["id"])
    for w in state.get("workers", []):
        ids.append(w["id"])
    if ids:
        api("POST", "/instance-operations/terminate", json={"instance_ids": ids})
        print(f"  Terminated: {ids}")
    STATE_FILE.unlink(missing_ok=True)
    print("==> Done.")


def cmd_status(args) -> None:
    state = load_state()
    if not state.get("server"):
        print("No state. Run 'create' first.")
        return
    srv = state["server"]
    print(f"Control plane: {srv['id']}  {srv.get('ip')}")
    for i, w in enumerate(state.get("workers", [])):
        print(f"Worker {i}: {w['id']}  {w.get('ip')}")
    if state.get("server_ip"):
        print(f"\n  ssh -i ~/.ssh/id_vastai ubuntu@{state['server_ip']} sudo kubectl get nodes")


def cmd_kubeconfig(args) -> None:
    state = load_state()
    if not state.get("server"):
        print("No state. Run 'create' first.")
        return
    ip = state["server"]["ip"]
    kubeconfig = ssh(ip, "sudo cat /etc/kubernetes/admin.conf", timeout=15)
    kubeconfig = kubeconfig.replace("server: https://", f"# original\n    server: https://").replace(
        "127.0.0.1", ip).replace("0.0.0.0", ip)
    # Simpler replace
    kubeconfig = ssh(ip, "sudo cat /etc/kubernetes/admin.conf", timeout=15)
    import re
    kubeconfig = re.sub(r'server: https://[^:]+:6443', f'server: https://{ip}:6443', kubeconfig)
    out = Path("kubeconfig")
    out.write_text(kubeconfig)
    print(f"Kubeconfig saved to ./kubeconfig")
    print(f"  export KUBECONFIG=$(pwd)/kubeconfig")
    print(f"  kubectl get nodes")


def main():
    parser = argparse.ArgumentParser(description="Lambda Labs kubeadm cluster lifecycle")
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
