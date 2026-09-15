"""Run an isolated, bounded remote batch and collect all its artifacts."""

from datetime import datetime
import os
from pathlib import Path
import shlex
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from run_closed_loop_activation_remote import download_tree, save


def main():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local = ROOT / "results/additional-ac" / stamp
    remote = "/home/runner/.aeroweaver-experiments/additional-ac/" + stamp
    local.mkdir(parents=True, exist_ok=False)
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.0.2.10", username="runner", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    ssh.get_transport().set_keepalive(30)
    code = None
    try:
        _, out, err = ssh.exec_command(shlex.join(["mkdir", "-p", remote]))
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        with ssh.open_sftp() as sftp:
            for name in ("run.py", "test_run.py", "preflight.py"):
                sftp.put(str(Path(__file__).with_name(name)), remote + "/" + name)
        python = "/home/runner/.venvs/aeroweaver/bin/python"
        prefix = ["env", "AEROWEAVER_REPO_ROOT=/home/runner/AeroWeaver", python, "-u"]
        inputs = "/home/runner/.aeroweaver-experiments/end-to-end-pilot/20260910-125042/inputs"
        status = "import json,urllib.request;s=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/status',timeout=10));v={k:s.get(k) for k in ['mission_active','is_executing','executing_robots','ai_executing']};print(json.dumps(v));assert not any(v.values())"
        commands = {
            "status": shlex.join(prefix + ["-c", status]),
            "tests": shlex.join(prefix + [remote + "/test_run.py"]),
            "batch": shlex.join(["timeout", "--signal=TERM", "--kill-after=15s", "21700s"] + prefix +
                                [remote + "/run.py", "--pilot-inputs", inputs, "--output", remote + "/outputs"]),
        }
        save(local / "launch.json", dict(remote=remote, commands=commands, planned_episodes=144))
        print("Artifacts: " + str(local), flush=True)
        for stage, command in commands.items():
            _, stdout, _ = ssh.exec_command(command + " 2>&1", timeout=22000)
            with (local / (stage + ".log")).open("w", encoding="utf-8") as log:
                for line in iter(stdout.readline, ""):
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)
            code = stdout.channel.recv_exit_status()
            if code:
                break
    finally:
        try:
            with ssh.open_sftp() as sftp:
                download_tree(sftp, remote, local / "remote-artifacts")
        finally:
            ssh.close()
            save(local / "exit.json", dict(exit_code=code))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
