"""Stage only the mission-parser changes, preserving unrelated live flight code."""

import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import time

import paramiko


ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/home/lsh/AeroWeaver"


def span(node):
    start = min([node.lineno, *[d.lineno for d in getattr(node, "decorator_list", [])]])
    return start - 1, node.end_lineno


def transplant(remote, local, names, class_name=None):
    def nodes(source):
        body = ast.parse(source).body
        if class_name:
            body = next(n for n in body if isinstance(n, ast.ClassDef) and n.name == class_name).body
        return {n.name: n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    old, new = nodes(remote), nodes(local)
    lines, local_lines = remote.splitlines(keepends=True), local.splitlines(keepends=True)
    edits = []
    for name in names:
        a, b = span(new[name])
        replacement = local_lines[a:b]
        if name in old:
            start, end = span(old[name])
        else:
            assert class_name
            _, start = span(old["__init__"])
            end = start
            replacement = ["\n", *replacement, "\n"]
        edits.append((start, end, replacement))
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        lines[start:end] = replacement
    result = "".join(lines)
    ast.parse(result)
    return result


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stage = f"/home/lsh/.aeroweaver-parser-stage-{stamp}"
    artifact = ROOT / "results" / "deployments" / f"llm-parser-{stamp}"
    artifact.mkdir(parents=True)
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("10.61.3.7", username="lsh", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    try:
        command = f"mkdir -p {shlex.quote(stage)} && cp -a {REMOTE}/backend {stage}/backend && cp -a {REMOTE}/tests {stage}/tests && cp {REMOTE}/pyproject.toml {stage}/pyproject.toml"
        _, out, err = ssh.exec_command(command, timeout=30)
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        entries = []
        with ssh.open_sftp() as sftp:
            for relative in ("backend/server.py", "backend/sim/mock_task_api.py", "backend/sim/mock_tasks.py"):
                with sftp.open(f"{REMOTE}/{relative}", "rb") as handle:
                    before = handle.read()
                live = before.decode("utf-8-sig").replace("\r\n", "\n")
                local = (ROOT / relative).read_text(encoding="utf-8-sig")
                if relative == "backend/server.py":
                    after = transplant(live, local, ["_run_commander_input"])
                elif relative == "backend/sim/mock_tasks.py":
                    after = transplant(live, local, [
                        "__init__", "_resolve_role_assignments", "agent_system_prompt", "observe", "snapshot",
                    ], "MockTask")
                else:
                    after = local
                target = artifact / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(after, encoding="utf-8", newline="\n")
                original = artifact / "before" / relative
                original.parent.mkdir(parents=True, exist_ok=True)
                original.write_bytes(before)
                sftp.put(str(target), f"{stage}/{relative}")
                entries.append({
                    "path": relative, "before_sha256": hashlib.sha256(before).hexdigest(),
                    "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                })
            for name in ("test_mock_tasks.py", "test_mock_task_parser.py"):
                sftp.put(str(ROOT / "tests" / name), f"{stage}/tests/{name}")
            # Read existing model settings in place; do not duplicate credential files.
            for name in (".env", ".aeroweaver_llm_config.json"):
                try:
                    sftp.stat(f"{REMOTE}/{name}")
                except FileNotFoundError:
                    continue
                sftp.symlink(f"{REMOTE}/{name}", f"{stage}/{name}")
        manifest = {"stage": stage, "root": REMOTE, "files": entries}
        (artifact / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps({"stage": stage, "artifact": str(artifact), "files": [x["path"] for x in entries]}))
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
