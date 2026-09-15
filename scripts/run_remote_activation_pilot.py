"""Prepare and run an isolated retrieval pilot on the authorized development host."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import sys
import time

import paramiko


ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/runner/.venvs/aeroweaver/bin/python"
VENV = "/home/runner/.venvs/aeroweaver-skill-eval"
BACKEND = "/home/runner/AeroWeaver/backend"


def execute(ssh, command, timeout=180, log=None):
    _, out, err = ssh.exec_command(command + " 2>&1", timeout=timeout)
    last = time.monotonic()
    while not out.channel.exit_status_ready() or out.channel.recv_ready():
        if out.channel.recv_ready():
            chunk = out.channel.recv(32768).decode("utf-8", errors="replace")
            print(chunk, end="", flush=True)
            if log:
                log.write(chunk)
                log.flush()
        else:
            time.sleep(0.2)
        if time.monotonic() - last > 30:
            print("[monitor] experiment command still active", flush=True)
            last = time.monotonic()
    status = out.channel.recv_exit_status()
    if status:
        raise RuntimeError(f"Remote command exited with code {status}; no automatic retry")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run-manifest", type=Path)
    args = parser.parse_args()
    if args.prepare == bool(args.run_manifest):
        parser.error("Choose --prepare or --run-manifest")
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.0.2.10", username="runner", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    try:
        if args.prepare:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            remote = f"/home/runner/.aeroweaver-experiments/skill-activation-{stamp}"
            local = ROOT / "results" / "skill-activation" / stamp
            local.mkdir(parents=True, exist_ok=False)
            execute(ssh, f"{PYTHON} -m venv --system-site-packages {VENV}")
            execute(ssh, f"{VENV}/bin/python -m pip install rank-bm25==0.2.2 tiktoken==0.12.0", timeout=240)
            files = list((ROOT / "experiments" / "skill_activation_v1").glob("*"))
            files += [ROOT / "tests" / "test_skill_activation_experiment.py"]
            hashes = {}
            with ssh.open_sftp() as sftp:
                for path in files:
                    if not path.is_file():
                        continue
                    relative = path.relative_to(ROOT).as_posix()
                    target = remote + "/" + relative
                    execute(ssh, "mkdir -p " + shlex.quote(str(PurePosixPath(target).parent)))
                    sftp.put(str(path), target)
                    data = path.read_bytes()
                    hashes[relative] = hashlib.sha256(data).hexdigest()
                    copy = local / "inputs" / relative
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    copy.write_bytes(data)
            manifest = {"remote": remote, "local": str(local), "hashes": hashes, "venv": VENV, "backend": BACKEND}
            (local / "launch.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            with (local / "unit-tests.log").open("w", encoding="utf-8") as log:
                execute(ssh, f"cd {remote} && PYTHONPATH={BACKEND} {VENV}/bin/python -m pytest tests/test_skill_activation_experiment.py -q", log=log)
            print(json.dumps({"prepared": True, "manifest": str(local / "launch.json")}))
        else:
            manifest = json.loads(args.run_manifest.read_text(encoding="utf-8"))
            local, remote = Path(manifest["local"]), manifest["remote"]
            if not local.resolve().is_relative_to(ROOT / "results" / "skill-activation"):
                raise ValueError("Local output is outside experiment results")
            if not remote.startswith("/home/runner/.aeroweaver-experiments/skill-activation-"):
                raise ValueError("Remote output is outside experiment namespace")
            with ssh.open_sftp() as sftp:
                for relative, expected in manifest["hashes"].items():
                    with sftp.open(remote + "/" + relative, "rb") as file:
                        assert hashlib.sha256(file.read()).hexdigest() == expected, "Input changed after preparation"
            command = (f"cd {shlex.quote(remote)} && timeout --signal=TERM --kill-after=10s 1800s "
                       f"{VENV}/bin/python experiments/skill_activation_v1/run.py "
                       f"--backend-root {BACKEND} --output {shlex.quote(remote + '/outputs')}")
            print("Executing isolated pilot with a 30-minute hard timeout", flush=True)
            try:
                with (local / "run.log").open("w", encoding="utf-8") as log:
                    execute(ssh, command, timeout=1830, log=log)
            finally:
                with ssh.open_sftp() as sftp:
                    try:
                        outputs = sftp.listdir(remote + "/outputs")
                    except FileNotFoundError:
                        outputs = []
                    for name in outputs:
                        sftp.get(remote + "/outputs/" + name, str(local / name))
            print(json.dumps({"completed": True, "local": str(local)}), flush=True)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
