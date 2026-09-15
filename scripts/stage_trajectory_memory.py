"""Stage the trajectory-memory patch against the live backend without changing it."""

import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import time

import paramiko


ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/home/runner/AeroWeaver"
PYTHON = "/home/runner/.venvs/aeroweaver/bin/python"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def transplant(before, after, name, parent=None):
    def find(source):
        nodes = ast.parse(source).body
        if parent:
            nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == parent).body
        return next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name)
    old, new = find(before), find(after)
    lines = before.splitlines(keepends=True)
    lines[old.lineno - 1:old.end_lineno] = after.splitlines(keepends=True)[new.lineno - 1:new.end_lineno]
    return "".join(lines)


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stage = f"/home/runner/.aeroweaver-memory-stage-{stamp}"
    local = ROOT / "results/memory-reward-alignment" / stamp
    local.mkdir(parents=True, exist_ok=False)
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.0.2.10", username="runner", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    try:
        code = ("import shutil,pathlib; "
                f"shutil.copytree('{REMOTE}/backend','{stage}/backend', "
                "ignore=shutil.ignore_patterns('__pycache__','data','*.jsonl','*.sqlite3','*.sqlite3-wal','*.sqlite3-shm')); "
                f"pathlib.Path('{stage}/tests').mkdir()")
        _, out, err = ssh.exec_command(PYTHON + " -c " + shlex.quote(code), timeout=120)
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        files = ["backend/memory/swarm_experience.py", "backend/memory/mock_trajectory.py",
                 "backend/sim/mock_rewards.py", "backend/sim/mock_task_api.py",
                 "backend/sim/mock_tasks.py", "backend/server.py", "requirements/mock.txt"]
        manifest = {"root": REMOTE, "stage": stage, "local": str(local), "files": []}
        with ssh.open_sftp() as sftp:
            for name in files:
                try:
                    with sftp.open(REMOTE + "/" + name, "rb") as handle:
                        before = handle.read()
                except FileNotFoundError:
                    before = None
                after = (ROOT / name).read_text(encoding="utf-8")
                if name.endswith("sim/mock_tasks.py"):
                    after = transplant(before.decode(), after, "snapshot", "MockTask")
                if name.endswith("backend/server.py"):
                    after = transplant(before.decode(), after, "_swarm_record_item")
                if name == "requirements/mock.txt":
                    after = before.decode() + "\nmpe2==1.1.0  # audited reward functions for Mock\n"
                data = after.encode()
                if name.endswith(".py"):
                    compile(after, name, "exec")
                saved = local / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(data)
                if before is not None:
                    backup = local / "before" / name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(before)
                target = stage + "/" + name
                try:
                    sftp.mkdir(str(Path(target).parent).replace("\\", "/"))
                except OSError:
                    pass
                sftp.put(str(saved), target)
                manifest["files"].append({"path": name, "before_sha256": digest(before) if before is not None else None,
                                          "after_sha256": digest(data)})
            for name in ("test_swarm_experience.py", "test_mock_rewards.py", "test_mock_task_parser.py",
                         "test_mock_tasks.py", "test_mission_progress.py"):
                sftp.put(str(ROOT / "tests" / name), stage + "/tests/" + name)
            (local / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            sftp.put(str(local / "manifest.json"), stage + "/manifest.json")
        print(json.dumps(manifest), flush=True)
        command = f"cd {shlex.quote(stage)} && PYTHONPATH={shlex.quote(stage + '/backend')} {PYTHON} -m pytest tests -q"
        _, out, err = ssh.exec_command(command, timeout=180)
        output = out.read().decode() + err.read().decode()
        (local / "tests.log").write_text(output, encoding="utf-8")
        print(output)
        if out.channel.recv_exit_status():
            raise RuntimeError("Staged tests failed; online backend untouched")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
