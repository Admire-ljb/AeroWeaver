"""Collect this development check's artifacts without modifying remote results."""

import os
from pathlib import Path
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from run_closed_loop_activation_remote import download_tree


def main():
    destination = ROOT / "results/end-to-end-preflight/20260910"
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.0.2.10", username="runner", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    try:
        with ssh.open_sftp() as sftp:
            for remote_name in ("end-to-end-probe-20260910a", "end-to-end-probe-20260910b", "end-to-end-offline-20260910a",
                                "end-to-end-offline-20260910b"):
                download_tree(sftp, "/home/runner/.aeroweaver-experiments/" + remote_name,
                              destination / remote_name)
            for relative in ("backend/adapters/mock_adapter.py", "backend/adapters/mock_dynamics.py",
                             "backend/sim/mock_tasks.py", "backend/sim/mock_rewards.py",
                             "backend/memory/swarm_experience.py"):
                target = destination / "runtime-snapshot" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                sftp.get("/home/runner/AeroWeaver/" + relative, str(target))
    finally:
        ssh.close()
    print(destination)


if __name__ == "__main__":
    main()
