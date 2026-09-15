"""Stage and run the authorized first batch without restarting the live app."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/home/runner/AeroWeaver"
PYTHON = "/home/runner/.venvs/aeroweaver/bin/python"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def execute(ssh, command, log_path):
    _, stdout, _ = ssh.exec_command(command + " 2>&1", timeout=7500)
    with log_path.open("w", encoding="utf-8") as log:
        for line in iter(stdout.readline, ""):
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
    return stdout.channel.recv_exit_status()


def download_tree(sftp, remote, local):
    local.mkdir(parents=True, exist_ok=True)
    for item in sftp.listdir_attr(remote):
        if item.filename in {".", "..", "__pycache__", ".pytest_cache"}:
            continue
        source = remote + "/" + item.filename
        target = local / item.filename
        if stat.S_ISDIR(item.st_mode):
            download_tree(sftp, source, target)
        elif stat.S_ISREG(item.st_mode):
            sftp.get(source, str(target))


def main():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local = ROOT / "results/skill-activation-closed-loop" / stamp
    local.mkdir(parents=True, exist_ok=False)
    remote = "/home/runner/.aeroweaver-experiments/skill-activation-closed-loop/" + stamp
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.0.2.10", username="runner", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    ssh.get_transport().set_keepalive(30)
    stage, exit_code = "status", None
    try:
        status_code = "import json,urllib.request;s=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/status',timeout=10));v={k:s.get(k) for k in ['mission_active','is_executing','executing_robots']};print(json.dumps(v));assert not any(v.values()),'Active mission: no changes made'"
        exit_code = execute(ssh, shlex.join([PYTHON, "-c", status_code]), local / "status-before.log")
        if exit_code:
            return
        _, out, err = ssh.exec_command("mkdir -p " + shlex.quote(remote + "/inputs"))
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        files = {"run.py": ROOT / "experiments/skill_activation_closed_loop_v1/run.py",
                 "test_run.py": ROOT / "experiments/skill_activation_closed_loop_v1/test_run.py",
                 "preregistration.md": ROOT / "experiments/skill_activation_closed_loop_v1/preregistration.md",
                 "catalog.json": ROOT / "experiments/skill_activation_v1/catalog.json"}
        with ssh.open_sftp() as sftp:
            for name, path in files.items():
                sftp.put(str(path), remote + "/inputs/" + name)
            dependency_path = "/home/runner/.venvs/aeroweaver-skill-eval/lib/python3.10/site-packages/rank_bm25.py"
            with sftp.open(dependency_path, "rb") as source:
                dependency = source.read()
            with sftp.open(remote + "/inputs/rank_bm25.py", "wb") as target:
                target.write(dependency)
        prefix = ["env", "AEROWEAVER_REPO_ROOT=" + REMOTE_ROOT,
                  "ACTIVATION_CATALOG=" + remote + "/inputs/catalog.json", PYTHON]
        run_args = ["-u", remote + "/inputs/run.py", "--catalog", remote + "/inputs/catalog.json"]
        commands = {
            "tests": shlex.join(prefix + ["-m", "pytest", "-q", remote + "/inputs/test_run.py", "--tb=short"]),
            "preflight": shlex.join(prefix + run_args + ["--preflight", "--output", remote + "/preflight",
                                    "--memory", remote + "/preflight.sqlite3"]),
            "batch": shlex.join(prefix + run_args + ["--output", remote + "/outputs", "--memory",
                                    REMOTE_ROOT + "/backend/data/swarm_experience/trajectories.sqlite3"]),
        }
        save(local / "launch.json", {"host": "192.0.2.10", "remote": remote, "commands": commands,
                                    "bm25_source": dependency_path,
                                    "bm25_sha256": hashlib.sha256(dependency).hexdigest(),
                                    "input_hashes": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}})
        print(f"Local artifacts: {local}\nRemote artifacts: {remote}", flush=True)
        for stage, command in commands.items():
            print("Stage: " + stage, flush=True)
            save(local / "stage.json", {"stage": stage, "status": "running"})
            exit_code = execute(ssh, command, local / (stage + ".log"))
            if exit_code:
                print(f"Stopped after {stage}; exit {exit_code}. No automatic retry.", flush=True)
                break
        execute(ssh, shlex.join([PYTHON, "-c", status_code]), local / "status-after.log")
    finally:
        with ssh.open_sftp() as sftp:
            try:
                download_tree(sftp, remote, local / "remote-artifacts")
            except FileNotFoundError:
                pass
        ssh.close()
        save(local / "exit.json", {"stage": stage, "exit_code": exit_code})
        print(f"Finished at stage={stage}, exit={exit_code}; artifacts={local}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
