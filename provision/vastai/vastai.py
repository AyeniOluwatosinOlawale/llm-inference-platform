"""
Vast.ai instance lifecycle management for the LLM inference platform.

Usage:
    python vastai.py create    # provision control plane + GPU workers, bootstrap k3s
    python vastai.py destroy   # tear down all instances
    python vastai.py status    # show running instances and k3s node status
    python vastai.py add-gpu   # add one GPU worker node
"""

import os
import sys
import json
import time
import subprocess
import argparse
import yaml
from pathlib import Path

try:
    import vastai as vastai_sdk
except ImportError:
    print("vastai SDK not installed. Run: pip install vastai")
    sys.exit(1)

CONFIG_FILE = Path(__file__).parent / "config.yaml"
STATE_FILE = Path(__file__).parent / "state.json"
CLOUD_INIT_DIR = Path(__file__).parent / "cloud-init"

API_KEY = os.environ.get("VASTAI_API_KEY")
if not API_KEY:
    print("Error: VASTAI_API_KEY environment variable not set")
    sys.exit(1)


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"control_plane": None, "gpu_workers": [], "k3s_token": None}


def save_state(state: dict) -> None:
    """
    Persist state locally; state.py mirrors this to Supabase Storage (FIX #3).
    """
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    # Mirror to remote
    try:
        from state import push_state
        push_state(state)
    except Exception as e:
        print(f"Warning: remote state push failed: {e}")


def build_gpu_query(cfg: dict) -> str:
    gpu_names = " ".join(f'"{g}"' for g in cfg["gpu_names"])
    return (
        f"gpu_name in [{gpu_names}] "
        f"gpu_ram >= {cfg['min_gpu_vram_gb'] * 1024} "
        f"num_gpus = {cfg['num_gpus']} "
        f"cpu_cores >= {cfg['min_cpu_cores']} "
        f"cpu_ram >= {cfg['min_ram_gb'] * 1024} "
        f"disk_space >= {cfg['min_disk_gb']} "
        f"rentable = true "
        f"reliability2 >= 0.95 "
        f"inet_up >= 500 inet_down >= 500"
    )


def search_offers(query: str) -> list[dict]:
    result = subprocess.run(
        ["vastai", "search", "offers", query, "--raw", "--limit", "10"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"vastai search failed: {result.stderr}")
    return json.loads(result.stdout)


def create_instance(offer_id: int, image: str, cloud_init_path: Path, label: str) -> dict:
    with open(cloud_init_path) as f:
        user_data = f.read()

    result = subprocess.run(
        [
            "vastai", "create", "instance", str(offer_id),
            "--image", image,
            "--disk", "100",
            "--env", f"VASTAI_LABEL={label}",
            "--onstart", "sh -c 'echo started'",
            "--raw",
        ],
        input=user_data,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"vastai create instance failed: {result.stderr}")
    return json.loads(result.stdout)


def wait_for_ssh(instance_id: int, timeout: int = 300) -> str:
    """Poll until instance is running and return SSH host:port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["vastai", "show", "instance", str(instance_id), "--raw"],
            capture_output=True, text=True
        )
        info = json.loads(result.stdout)
        if info.get("actual_status") == "running" and info.get("ssh_host"):
            return f"{info['ssh_host']}:{info['ssh_port']}"
        print(f"  Waiting for instance {instance_id}... ({info.get('actual_status', 'unknown')})")
        time.sleep(15)
    raise TimeoutError(f"Instance {instance_id} did not become ready within {timeout}s")


def destroy_instance(instance_id: int) -> None:
    subprocess.run(
        ["vastai", "destroy", "instance", str(instance_id)],
        check=True
    )


def cmd_create(args) -> None:
    cfg = load_config()
    state = load_state()

    print("==> Searching for control plane offer...")
    # Control plane can be a cheap CPU instance
    cp_cfg = cfg["control_plane"]
    cp_query = (
        f"num_gpus = 0 cpu_cores >= {cp_cfg['min_cpu_cores']} "
        f"cpu_ram >= {cp_cfg['min_ram_gb'] * 1024} "
        f"disk_space >= {cp_cfg['min_disk_gb']} rentable = true"
    )
    cp_offers = search_offers(cp_query)
    if not cp_offers:
        raise RuntimeError("No suitable control plane offers found")

    cp_offer = sorted(cp_offers, key=lambda x: x["dph_total"])[0]
    print(f"    Selected offer {cp_offer['id']} @ ${cp_offer['dph_total']:.4f}/hr")

    print("==> Creating control plane instance...")
    cp_inst = create_instance(
        cp_offer["id"], cp_cfg["image"],
        CLOUD_INIT_DIR / "server.yaml", "k3s-server"
    )
    state["control_plane"] = cp_inst
    save_state(state)

    print(f"==> Waiting for control plane {cp_inst['new_contract']}...")
    cp_ssh = wait_for_ssh(cp_inst["new_contract"])
    state["control_plane"]["ssh"] = cp_ssh
    save_state(state)

    print("==> Fetching k3s join token...")
    time.sleep(60)  # allow k3s server to start
    token_result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-p",
         cp_ssh.split(":")[1], f"root@{cp_ssh.split(':')[0]}",
         "cat /var/lib/rancher/k3s/server/node-token"],
        capture_output=True, text=True
    )
    k3s_token = token_result.stdout.strip()
    state["k3s_token"] = k3s_token
    state["k3s_server_ip"] = cp_ssh.split(":")[0]
    save_state(state)

    print(f"==> Provisioning {cfg['gpu_workers']['count']} GPU worker(s)...")
    worker_cfg = cfg["gpu_workers"]
    gpu_offers = search_offers(build_gpu_query(worker_cfg))
    if not gpu_offers:
        raise RuntimeError("No suitable GPU offers found")

    gpu_offers_sorted = sorted(gpu_offers, key=lambda x: x["dph_total"])
    for i in range(worker_cfg["count"]):
        offer = gpu_offers_sorted[i % len(gpu_offers_sorted)]
        print(f"  Worker {i+1}: offer {offer['id']} @ ${offer['dph_total']:.4f}/hr ({offer.get('gpu_name','?')})")

        # Inject k3s join info into cloud-init
        agent_init = (CLOUD_INIT_DIR / "agent.yaml").read_text()
        agent_init = agent_init.replace("__K3S_URL__", f"https://{state['k3s_server_ip']}:6443")
        agent_init = agent_init.replace("__K3S_TOKEN__", k3s_token)
        tmp_init = Path(f"/tmp/agent-{i}.yaml")
        tmp_init.write_text(agent_init)

        worker_inst = create_instance(
            offer["id"], worker_cfg["image"], tmp_init, f"k3s-agent-{i}"
        )
        state["gpu_workers"].append(worker_inst)
        save_state(state)

    print("\n==> Cluster provisioned. Fetch kubeconfig:")
    print(f"    ssh -p {cp_ssh.split(':')[1]} root@{cp_ssh.split(':')[0]} cat /etc/rancher/k3s/k3s.yaml > kubeconfig")
    print(f"    sed -i 's/127.0.0.1/{cp_ssh.split(':')[0]}/' kubeconfig")
    print("    export KUBECONFIG=$(pwd)/kubeconfig")


def cmd_destroy(args) -> None:
    state = load_state()
    ids = []
    if state.get("control_plane") and state["control_plane"].get("new_contract"):
        ids.append(state["control_plane"]["new_contract"])
    for w in state.get("gpu_workers", []):
        if w.get("new_contract"):
            ids.append(w["new_contract"])

    for inst_id in ids:
        print(f"==> Destroying instance {inst_id}...")
        destroy_instance(inst_id)

    STATE_FILE.unlink(missing_ok=True)
    print("==> All instances destroyed and state cleared.")


def cmd_status(args) -> None:
    state = load_state()
    if not state.get("control_plane"):
        print("No cluster state found. Run 'create' first.")
        return
    cp_id = state["control_plane"].get("new_contract")
    print(f"Control plane: instance {cp_id}  SSH: {state['control_plane'].get('ssh', 'unknown')}")
    for i, w in enumerate(state.get("gpu_workers", [])):
        print(f"GPU worker {i}: instance {w.get('new_contract')}  SSH: {w.get('ssh', 'pending')}")


def main():
    parser = argparse.ArgumentParser(description="Vast.ai cluster lifecycle management")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("create")
    sub.add_parser("destroy")
    sub.add_parser("status")
    sub.add_parser("add-gpu")
    args = parser.parse_args()

    commands = {"create": cmd_create, "destroy": cmd_destroy, "status": cmd_status}
    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
