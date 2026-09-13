"""Run the bounded collection pilot on the authorized development host."""

from datetime import datetime
import json
import os
from pathlib import Path
import shlex

import paramiko


ROOT = Path(__file__).resolve().parents[1]


def main():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local = ROOT / "results/memory-collection" / stamp
    remote = "/home/lsh/.aeroweaver-experiments/memory-collection/" + stamp
    local.mkdir(parents=True, exist_ok=False)
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("10.61.3.7", username="lsh", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    exit_code = None
    try:
        _, out, err = ssh.exec_command("mkdir -p " + shlex.quote(remote))
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        with ssh.open_sftp() as sftp:
            sftp.put(str(ROOT / "scripts/run_memory_collection_pilot.py"), remote + "/run.py")
        command = "/home/lsh/.venvs/aeroweaver/bin/python -u " + shlex.quote(remote + "/run.py")
        command += " --output " + shlex.quote(remote + "/outputs") + " 2>&1"
        (local / "launch.json").write_text(json.dumps({"host": "10.61.3.7", "command": command,
                                                     "remote": remote}, indent=2) + "\n")
        print(f"Local artifacts: {local}\nRemote artifacts: {remote}", flush=True)
        _, out, _ = ssh.exec_command(command, timeout=750)
        with (local / "run.log").open("w", encoding="utf-8") as log:
            for line in iter(out.readline, ""):
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
        exit_code = out.channel.recv_exit_status()
    finally:
        with ssh.open_sftp() as sftp:
            for name in ("plan.json", "progress.json", "report.json"):
                try:
                    sftp.get(remote + "/outputs/" + name, str(local / name))
                except FileNotFoundError:
                    pass
            report_path = local / "report.json"
            if report_path.exists():
                report = json.loads(report_path.read_text())
                for item in report["started_missions"]:
                    mission = item["mission_id"]
                    if not mission.startswith("mock-") or not mission.replace("-", "").isalnum():
                        raise ValueError("Unexpected mission identifier")
                    directory = local / "episodes" / mission
                    directory.mkdir(parents=True, exist_ok=True)
                    for name in ("summary.json", "experience.jsonl", "trace.jsonl", "reward_manifest.json"):
                        try:
                            sftp.get("/home/lsh/AeroWeaver/results/mock-tasks/" + mission + "/" + name,
                                     str(directory / name))
                        except FileNotFoundError:
                            print(f"Not yet available: {mission}/{name}", flush=True)
        ssh.close()
        (local / "exit.json").write_text(json.dumps({"exit_code": exit_code}) + "\n")
        print(f"Collection process exit={exit_code}; artifacts={local}", flush=True)
    raise SystemExit(exit_code if exit_code is not None else 1)


if __name__ == "__main__":
    main()
