"""Targeted deployment/commands for the explicitly configured development host."""

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
REMOTE = "/home/lsh/AeroWeaver"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", nargs="*", default=[])
    parser.add_argument("--command")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--download", nargs=2, metavar=("REMOTE_PATH", "LOCAL_PATH"))
    args = parser.parse_args()
    password = os.environ.get("AEROWEAVER_DEPLOY_PASSWORD")
    if not password:
        parser.error("AEROWEAVER_DEPLOY_PASSWORD is required")
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    # Matches the existing explicitly authorized LAN deployment helper.
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("10.61.3.7", username="lsh", password=password, timeout=15)
    try:
        sftp = ssh.open_sftp()
        try:
            files = []
            for item in args.upload:
                path = (ROOT / item).resolve()
                if not path.is_relative_to(ROOT) or path.name.startswith(".env"):
                    raise ValueError("Only non-secret project files may be deployed")
                files.extend(path.rglob("*") if path.is_dir() else [path])
            files = [p for p in files if p.is_file() and "__pycache__" not in p.parts]
            if files:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                backup = f"/home/lsh/.aeroweaver-mpe-backup-{stamp}"
                manifest = []
                for path in files:
                    relative = path.relative_to(ROOT).as_posix()
                    target = REMOTE + "/" + relative
                    command = f"mkdir -p {shlex.quote(str(PurePosixPath(target).parent))} {shlex.quote(str(PurePosixPath(backup + '/' + relative).parent))}"
                    _, out, err = ssh.exec_command(command)
                    if out.channel.recv_exit_status():
                        raise RuntimeError(err.read().decode())
                    before = None
                    try:
                        with sftp.open(target, "rb") as handle:
                            original = handle.read()
                        before = hashlib.sha256(original).hexdigest()
                        with sftp.open(backup + "/" + relative, "wb") as handle:
                            handle.write(original)
                    except FileNotFoundError:
                        pass
                    sftp.put(str(path), target + ".mpe-upload")
                    sftp.posix_rename(target + ".mpe-upload", target)
                    manifest.append({"path": relative, "before_sha256": before, "after_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                with sftp.open(backup + "/manifest.json", "w") as handle:
                    handle.write(json.dumps(manifest, indent=2))
                print(f"Uploaded {len(files)} files; backup={backup}", flush=True)
            if args.download:
                source, dest = args.download
                local = Path(dest).resolve()
                if not local.is_relative_to(ROOT):
                    raise ValueError("Download must stay inside the local project")
                local.parent.mkdir(parents=True, exist_ok=True)
                sftp.get(source, str(local))
                print(f"Downloaded {source} to {local}")
        finally:
            sftp.close()
        if args.command:
            _, out, err = ssh.exec_command(args.command, timeout=args.timeout)
            for line in iter(out.readline, ""):
                print(line, end="", flush=True)
            error = err.read().decode(errors="replace")
            if error:
                print(error, file=sys.stderr)
            raise SystemExit(out.channel.recv_exit_status())
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
