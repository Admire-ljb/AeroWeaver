"""Launch only one HMAS-2 World Communication episode on the existing host."""

from datetime import datetime
import os
from pathlib import Path
import shlex
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from run_closed_loop_activation_remote import download_tree, execute, save

PYTHON = "/home/runner/.venvs/aeroweaver/bin/python"
REFERENCE = "/home/runner/.aeroweaver-experiments/end-to-end-pilot/20260910-125042"


def main():
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("192.0.2.10", username="runner", password=os.environ.get("AEROWEAVER_DEPLOY_PASSWORD"),
                    timeout=10, auth_timeout=10, banner_timeout=10)
    except paramiko.SSHException as exc:
        ssh.close()
        raise SystemExit(f"SSH authentication/connection unavailable; no experiment started: {exc}")
    ssh.get_transport().set_keepalive(30)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local = ROOT / "results/world-hmas-single-recheck" / stamp
    remote = "/home/runner/.aeroweaver-experiments/world-hmas-single-recheck/" + stamp
    local.mkdir(parents=True, exist_ok=False)
    code = None
    try:
        status = (
            "import json,urllib.request;"
            "s=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/status',timeout=10));"
            "v={k:s.get(k) for k in ['mission_active','is_executing','executing_robots']};"
            "print(json.dumps(v));assert not any(v.values()),'Live mission active'"
        )
        if execute(ssh, shlex.join([PYTHON, "-c", status]), local / "status-before.log"):
            raise RuntimeError("Live mission active or status unavailable; no episode started")
        if execute(ssh, shlex.join(["mkdir", "-p", remote]), local / "stage.log"):
            raise RuntimeError("Cannot create isolated experiment directory")
        with ssh.open_sftp() as sftp:
            sftp.put(str(Path(__file__).with_name("run_world_hmas_recheck.py")), remote + "/run_one.py")
        command = shlex.join(["timeout", "--signal=TERM", "--kill-after=10s", "950s", "env",
                              "AEROWEAVER_REPO_ROOT=/home/runner/AeroWeaver", PYTHON, "-u",
                              remote + "/run_one.py", "--pilot-inputs", REFERENCE + "/inputs",
                              "--reference-output", REFERENCE + "/outputs", "--output", remote + "/outputs"])
        save(local / "launch.json", {"remote": remote, "command": command, "planned_episodes": 1})
        print("Artifacts: " + str(local), flush=True)
        code = execute(ssh, command, local / "run.log")
    finally:
        try:
            with ssh.open_sftp() as sftp:
                try:
                    download_tree(sftp, remote, local / "remote-artifacts")
                except FileNotFoundError:
                    pass
        finally:
            ssh.close()
            save(local / "exit.json", {"exit_code": code})
    return code


if __name__ == "__main__":
    raise SystemExit(main())
