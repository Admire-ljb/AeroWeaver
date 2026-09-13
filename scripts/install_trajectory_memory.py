"""Install the tested trajectory-memory bundle, preserving existing service settings."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.request


import sys

STAGE = Path(sys.argv[1]).resolve()
ROOT = Path("/home/lsh/AeroWeaver")


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:5001" + path, timeout=5) as response:
        return json.load(response)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_ready():
    for _ in range(60):
        try:
            if get("/api/status").get("initialized"):
                assert get("/api/adapter/status")["adapter"] == "mock"
                return
        except (OSError, AssertionError):
            pass
        time.sleep(1)
    raise RuntimeError("Backend did not become ready")


def stop(pid):
    os.kill(pid, signal.SIGTERM)
    for _ in range(150):
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists() or stat.read_text().split(")", 1)[1].split()[0] == "Z":
            return
        time.sleep(0.1)
    raise RuntimeError("Backend did not exit after SIGTERM; no files replaced")


def main():
    manifest = json.loads((STAGE / "manifest.json").read_text())
    assert STAGE == Path(manifest["stage"])
    assert manifest["root"] == str(ROOT)
    assert get("/api/adapter/status")["adapter"] == "mock"
    status = get("/api/status")
    if status["mission_active"] or status["is_executing"] or status["executing_robots"]:
        raise RuntimeError("A task is active; refusing to interrupt it")
    (STAGE / "previous-task.json").write_text(json.dumps(get("/api/mock/tasks")["current"], indent=2))
    listeners = subprocess.check_output(["ss", "-ltnp", "sport = :5001"], text=True)
    pids = set(re.findall(r"pid=(\d+)", listeners))
    assert len(pids) == 1
    pid = int(pids.pop())
    proc = Path(f"/proc/{pid}")
    assert (proc / "cwd").resolve() == ROOT
    command = [os.fsdecode(x) for x in (proc / "cmdline").read_bytes().split(b"\0") if x]
    assert command == ["/home/lsh/.venvs/aeroweaver/bin/python", "backend/server.py"]
    environ = dict(os.fsdecode(x).split("=", 1) for x in (proc / "environ").read_bytes().split(b"\0") if x)
    backup = STAGE / "backup"
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        assert (sha(ROOT / relative) if (ROOT / relative).exists() else None) == entry["before_sha256"], f"Live file changed: {relative}"
        assert sha(STAGE / relative) == entry["after_sha256"], f"Stage changed: {relative}"
        if relative.suffix == ".py":
            compile((STAGE / relative).read_bytes(), str(relative), "exec")
        saved = backup / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        if (ROOT / relative).exists():
            saved.write_bytes((ROOT / relative).read_bytes())

    def start():
        with (ROOT / "logs/server.log").open("ab", buffering=0) as log:
            return subprocess.Popen(command, cwd=ROOT, env=environ, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True)

    child = None
    stop(pid)
    try:
        for entry in manifest["files"]:
            target = ROOT / entry["path"]
            temp = target.with_name(target.name + ".memory-deploy")
            temp.write_bytes((STAGE / entry["path"]).read_bytes())
            if target.exists():
                temp.chmod(target.stat().st_mode)
            temp.replace(target)
        child = start()
        wait_ready()
    except Exception:
        if child is not None and child.poll() is None:
            stop(child.pid)
        for entry in manifest["files"]:
            target = ROOT / entry["path"]
            assert sha(target) in {entry["before_sha256"], entry["after_sha256"]}
            if entry["before_sha256"] is None:
                target.unlink()
            else:
                target.write_bytes((backup / entry["path"]).read_bytes())
        start()
        wait_ready()
        raise
    result = {"installed": True, "previous_pid": pid, "pid": child.pid,
              "backup": str(backup), "files": manifest["files"], "status": get("/api/status"),
              "memory": get("/api/memory/swarm-experience?limit=1")["stats"]}
    (STAGE / "deployment-result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
