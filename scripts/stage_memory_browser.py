"""Stage the episode browser against current deployed sources, preserving other edits."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat

import paramiko


ROOT = Path(__file__).resolve().parents[1]
LIVE = "/home/lsh/AeroWeaver"
RUN = ROOT / "results/memory-browser/20260909-124908"
STAGE = "/home/lsh/.aeroweaver-memory-browser-20260909-124908"
CHANGED = ["backend/server.py", "backend/memory/trajectory_api.py", "frontend/src/App.jsx",
           "frontend/src/components/TrajectoryMemoryWorkspace.jsx",
           "frontend/src/components/TrajectoryMemoryWorkspace.css", "frontend/package.json", "frontend/package-lock.json"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "publish"])
    args = parser.parse_args()
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("10.61.3.7", username="lsh", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    try:
        with ssh.open_sftp() as sftp:
            def run(command):
                _, out, err = ssh.exec_command(command, timeout=180)
                result, errors = out.read().decode(), err.read().decode()
                print(result, end="", flush=True)
                if errors:
                    print(errors, flush=True)
                if out.channel.recv_exit_status():
                    raise RuntimeError("Remote command failed")
                return result

            def upload(source, relative):
                target = STAGE + "/" + relative
                run("mkdir -p " + shlex.quote(str(Path(target).parent).replace("\\", "/")))
                sftp.put(str(source), target)

            candidate = RUN / "candidate"
            if args.mode == "prepare":
                candidate.mkdir(parents=True, exist_ok=False)
                def download_tree(remote, local):
                    local.mkdir(parents=True, exist_ok=True)
                    for item in sftp.listdir_attr(remote):
                        if item.filename in {"node_modules", "dist", ".git", "__pycache__"} or item.filename.startswith(".env"):
                            continue
                        dest = local / item.filename
                        if stat.S_ISDIR(item.st_mode):
                            download_tree(remote + "/" + item.filename, dest)
                        elif stat.S_ISREG(item.st_mode):
                            sftp.get(remote + "/" + item.filename, str(dest))
                download_tree(LIVE + "/frontend", candidate / "frontend")
                (candidate / "backend").mkdir()
                sftp.get(LIVE + "/backend/server.py", str(candidate / "backend/server.py"))
                before = {}
                for relative in CHANGED:
                    try:
                        with sftp.open(LIVE + "/" + relative, "rb") as handle:
                            before[relative] = sha(handle.read())
                    except FileNotFoundError:
                        before[relative] = None
                (RUN / "before-hashes.json").write_text(json.dumps(before, indent=2))
                for relative in CHANGED:
                    if relative not in {"backend/server.py", "frontend/src/App.jsx"}:
                        (candidate / relative).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(ROOT / relative, candidate / relative)
                app = candidate / "frontend/src/App.jsx"
                text = app.read_text(encoding="utf-8")
                start = text.index("function MemoryWorkspace(")
                end = text.index("function CapabilityWorkspace(", start)
                text = text[:start] + "function MemoryWorkspace({ language }) {\n  return <TrajectoryMemoryWorkspace language={language} apiBase={API_BASE} />\n}\n\n" + text[end:]
                text = "import TrajectoryMemoryWorkspace from './components/TrajectoryMemoryWorkspace'\n" + text
                app.write_text(text, encoding="utf-8")
                server = candidate / "backend/server.py"
                text = server.read_text(encoding="utf-8")
                marker = '@app.route("/api/environments/mpe", methods=["GET"])'
                assert text.count(marker) == 1 and "register_trajectory_api" not in text
                text = text.replace(marker, "from memory.trajectory_api import register_trajectory_api\nregister_trajectory_api(app, _get_swarm_experience_memory)\n\n\n" + marker)
                server.write_text(text, encoding="utf-8")
                command = "import shutil; shutil.copytree(" + repr(LIVE + "/backend") + "," + repr(STAGE + "/backend")
                command += ",ignore=shutil.ignore_patterns('data','__pycache__','logs'))"
                run("/home/lsh/.venvs/aeroweaver/bin/python -c " + shlex.quote(command))
                for relative in ("backend/server.py", "backend/memory/trajectory_api.py"):
                    upload(candidate / relative, relative)
                for name in ("test_trajectory_api.py", "test_swarm_experience.py"):
                    upload(ROOT / "tests" / name, "tests/" + name)
                command = f"cd {shlex.quote(STAGE)} && PYTHONPATH={shlex.quote(STAGE + '/backend')} /home/lsh/.venvs/aeroweaver/bin/python -m pytest tests -q"
                result = run(command)
                (RUN / "tests.log").write_text(result)
            else:
                before = json.loads((RUN / "before-hashes.json").read_text())
                files = CHANGED + [p.relative_to(candidate).as_posix() for p in (candidate / "frontend/dist").rglob("*") if p.is_file()]
                assert any(name.endswith("dist/index.html") for name in files)
                entries = []
                for relative in files:
                    try:
                        with sftp.open(LIVE + "/" + relative, "rb") as handle:
                            live_hash = sha(handle.read())
                    except FileNotFoundError:
                        live_hash = None
                    if relative in before:
                        assert live_hash == before[relative], "Live file changed: " + relative
                    upload(candidate / relative, relative)
                    entries.append({"path": relative, "before_sha256": live_hash, "after_sha256": sha((candidate / relative).read_bytes())})
                manifest = {"stage": STAGE, "root": LIVE, "files": entries}
                (RUN / "manifest.json").write_text(json.dumps(manifest, indent=2))
                upload(RUN / "manifest.json", "manifest.json")
                upload(ROOT / "scripts/install_trajectory_memory.py", "install.py")
                result = run(f"/home/lsh/.venvs/aeroweaver/bin/python {shlex.quote(STAGE + '/install.py')} {shlex.quote(STAGE)}")
                (RUN / "deploy.log").write_text(result)
                sftp.get(STAGE + "/deployment-result.json", str(RUN / "deployment-result.json"))
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
