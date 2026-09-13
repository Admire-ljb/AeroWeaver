"""Read-only post-run source and process check on the authorized experiment host."""

import argparse
import json
import os
from pathlib import Path
import shlex
import paramiko


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    plan = json.loads((args.run / "remote-artifacts/outputs/plan.json").read_text())
    remote = json.loads((args.run / "launch.json").read_text())["remote"]
    script = """
import hashlib,json,pathlib,subprocess,urllib.request
root=pathlib.Path('/home/lsh/AeroWeaver')
expected=EXPECTED
actual={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in expected}
status=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/status',timeout=10))
processes=subprocess.check_output(['ps','-eo','pid,args'],text=True).splitlines()
running=[line for line in processes if REMOTE+'/inputs/run_all_tasks.py' in line and '-c ' not in line]
print(json.dumps({'source_hashes_unchanged':actual==expected,'actual_hashes':actual,'experiment_processes':running,
                  'live_app':{k:status.get(k) for k in ['mission_active','is_executing','executing_robots']}}))
""".replace("EXPECTED", repr(plan["source_hashes"])).replace("REMOTE", repr(remote))
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("10.61.3.7", username="lsh", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    try:
        _, out, err = ssh.exec_command(shlex.join(["/home/lsh/.venvs/aeroweaver/bin/python", "-c", script]), timeout=30)
        body = out.read().decode()
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        result = json.loads(body)
        (args.run / "runtime-after.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        path = args.run / "remote-artifacts/outputs/completion.json"
        completion = json.loads(path.read_text())
        completion["source_hashes_unchanged"] = result["source_hashes_unchanged"]
        path.write_text(json.dumps(completion, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result))
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
