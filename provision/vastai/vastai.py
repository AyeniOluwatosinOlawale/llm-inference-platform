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
    gpu_names = "[" + ",".join(f'"{g}"' for g in cfg["gpu_names"]) + "]"
    # Note: reliability2 is no longer a valid Vast.ai search field — omitted.
    # gpu_ram filter removed: large integer values are buggy in Vast.ai query parser.
    # Filter by gpu_name instead to ensure VRAM requirements.
    return (
        f"gpu_name in {gpu_names} "
        f"num_gpus >= {cfg['num_gpus']} "
        f"cpu_cores >= {cfg['min_cpu_cores']} "
        f"disk_space >= {cfg['min_disk_gb']} "
        f"rentable=true "
        f"inet_up >= 1000 inet_down >= 1000"
    )


def vastai_cmd(*args) -> list[str]:
    """Build a vastai CLI command with the API key injected."""
    return ["vastai", "--api-key", API_KEY] + list(args)


def search_offers(query: str) -> list[dict]:
    result = subprocess.run(
        vastai_cmd("search", "offers", query, "--raw", "--limit", "20"),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"vastai search failed: {result.stderr}")
    try:
        # Strip any warning lines before the JSON array
        stdout = result.stdout
        idx = stdout.find("[")
        if idx > 0:
            stdout = stdout[idx:]
        return json.loads(stdout)
    except json.JSONDecodeError:
        return []


def create_instance(offer_id: int, image: str, cloud_init_path: Path, label: str) -> dict:
    ssh_pub_key = Path(os.path.expanduser("~/.ssh/id_vastai.pub")).read_text().strip()
    onstart_script = cloud_init_path.read_text()
    # Embed the SSH public key directly into the onstart script so it is
    # available without relying on env var injection (which Vast.ai does not
    # reliably propagate into running containers).
    onstart_script = onstart_script.replace("__SSH_PUBLIC_KEY__", ssh_pub_key)
    # Pass SSH_PUBLIC_KEY + expose Jupyter (8080) for fallback access
    # cap-add flags grant Linux capabilities needed for k3s/kubelet inside Vast.ai containers
    env_str = f"--cap-add SYS_ADMIN --cap-add NET_ADMIN --cap-add NET_RAW -e SSH_PUBLIC_KEY={ssh_pub_key} -p 8080:8080"
    result = subprocess.run(
        vastai_cmd(
            "create", "instance", str(offer_id),
            "--image", image,
            "--disk", "100",
            "--ssh", "--direct",
            "--jupyter",
            "--label", label,
            "--env", env_str,
            "--onstart-cmd", onstart_script,
            "--raw",
        ),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"vastai create instance failed: {result.stderr}")
    try:
        stdout = result.stdout
        idx = stdout.find("{")
        if idx > 0:
            stdout = stdout[idx:]
        data = json.loads(stdout)
        if not data.get("success"):
            raise RuntimeError(f"vastai create instance failed: {data}")
        return data
    except json.JSONDecodeError:
        raise RuntimeError(f"vastai create instance unexpected output: {result.stdout}\n{result.stderr}")


def wait_for_ssh(instance_id: int, timeout: int = 900) -> str:
    """Poll until instance is running and return SSH host:port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            vastai_cmd("show", "instance", str(instance_id), "--raw"),
            capture_output=True, text=True
        )
        stdout = result.stdout
        idx = stdout.find("{")
        if idx > 0:
            stdout = stdout[idx:]
        try:
            info = json.loads(stdout)
        except json.JSONDecodeError:
            info = {}
        if info.get("actual_status") == "running" and info.get("ssh_host"):
            # Prefer direct connection over Vast.ai proxy (proxy resets connections)
            ports = info.get("ports") or {}
            direct = ports.get("22/tcp", [{}])[0].get("HostPort")
            public_ip = info.get("public_ipaddr")
            if direct and public_ip:
                return f"{public_ip}:{direct}"
            return f"{info['ssh_host']}:{info['ssh_port']}"
        print(f"  Waiting for instance {instance_id}... ({info.get('actual_status', 'unknown')})")
        time.sleep(15)
    raise TimeoutError(f"Instance {instance_id} did not become ready within {timeout}s")


def destroy_instance(instance_id: int) -> None:
    subprocess.run(
        vastai_cmd("destroy", "instance", str(instance_id)),
        check=True
    )


def cmd_create(args) -> None:
    cfg = load_config()
    state = load_state()

    # Use a GPU node as control plane — avoids unavailable cheap CPU-only instances on Vast.ai
    worker_cfg = cfg["gpu_workers"]
    print("==> Searching for GPU offers...")
    gpu_offers = search_offers(build_gpu_query(worker_cfg))
    if not gpu_offers:
        raise RuntimeError("No suitable GPU offers found")

    gpu_offers_sorted = sorted(gpu_offers, key=lambda x: x["dph_total"])
    used_offer_ids = set()

    # First GPU node = k3s server (control plane + worker)
    print("==> Provisioning control plane GPU node...")
    cp_inst = None
    for offer in gpu_offers_sorted:
        print(f"    Trying offer {offer['id']} @ ${offer['dph_total']:.4f}/hr ({offer.get('gpu_name','?')})...")
        try:
            cp_inst = create_instance(
                offer["id"], worker_cfg["image"],
                CLOUD_INIT_DIR / "server.sh", "k3s-server"
            )
            used_offer_ids.add(offer["id"])
            break
        except RuntimeError as e:
            print(f"    Unavailable: {e}")
    if not cp_inst:
        raise RuntimeError("All GPU offers failed — check your Vast.ai account balance and payment method")

    state["control_plane"] = cp_inst
    save_state(state)

    print(f"==> Waiting for control plane {cp_inst['new_contract']} to start...")
    cp_ssh = wait_for_ssh(cp_inst["new_contract"])
    state["control_plane"]["ssh"] = cp_ssh
    save_state(state)

    # Wait for k3s bootstrap script to complete (installs k3s, ~5 min)
    print("==> Waiting for k3s server bootstrap (5 min)...")
    time.sleep(300)

    # Poll until the node token file exists
    cp_host, cp_port = cp_ssh.rsplit(":", 1)
    ssh_base = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "PubkeyAuthentication=unbound",
                "-i", str(Path.home() / ".ssh" / "id_vastai"), "-p", cp_port, f"root@{cp_host}"]
    print("==> Fetching k3s node token...")
    k3s_token = ""
    for attempt in range(20):
        token_result = subprocess.run(
            ssh_base + ["cat /var/lib/rancher/k3s/server/node-token"],
            capture_output=True, text=True
        )
        k3s_token = token_result.stdout.strip()
        if k3s_token:
            break
        print(f"  k3s not ready yet, waiting 30s (attempt {attempt+1}/20)...")
        time.sleep(30)
    if not k3s_token:
        raise RuntimeError("k3s token never appeared — bootstrap may have failed. Check /var/log/k3s-bootstrap.log on the instance.")

    # Get the public IP of the control plane for the agent to join
    ip_result = subprocess.run(
        ssh_base + ["bash", "-c", "curl -s ifconfig.me || curl -s api.ipify.org"],
        capture_output=True, text=True
    )
    k3s_server_ip = ip_result.stdout.strip() or cp_host
    state["k3s_token"] = k3s_token
    state["k3s_server_ip"] = k3s_server_ip
    save_state(state)

    # Remaining GPU nodes = k3s agents
    remaining_offers = [o for o in gpu_offers_sorted if o["id"] not in used_offer_ids]
    for i in range(worker_cfg["count"] - 1):
        # Inject k3s join info into bootstrap shell script
        agent_init = (CLOUD_INIT_DIR / "agent.sh").read_text()
        agent_init = agent_init.replace("__K3S_URL__", f"https://{state['k3s_server_ip']}:6443")
        agent_init = agent_init.replace("__K3S_TOKEN__", k3s_token)
        tmp_init = Path(f"/tmp/agent-{i}.sh")
        tmp_init.write_text(agent_init)

        worker_inst = None
        for offer in remaining_offers:
            print(f"  Worker {i+1}: trying offer {offer['id']} @ ${offer['dph_total']:.4f}/hr ({offer.get('gpu_name','?')})")
            try:
                worker_inst = create_instance(
                    offer["id"], worker_cfg["image"], tmp_init, f"k3s-agent-{i}"
                )
                remaining_offers.remove(offer)
                break
            except RuntimeError as e:
                print(f"    Offer unavailable, trying next... ({e})")
        if not worker_inst:
            raise RuntimeError(f"Could not provision GPU worker {i+1} — no offers available")
        state["gpu_workers"].append(worker_inst)
        save_state(state)

    print("\n==> Cluster provisioned. Fetch kubeconfig:")
    print(f"    ssh -i ~/.ssh/id_vastai -p {cp_port} root@{cp_host} cat /etc/rancher/k3s/k3s.yaml > kubeconfig")
    print(f"    sed -i 's/127.0.0.1/{k3s_server_ip}/' kubeconfig")
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
