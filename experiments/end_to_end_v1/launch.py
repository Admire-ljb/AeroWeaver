"""Stage, test and run the authorized bounded pilot in an isolated directory."""

from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import shlex
import sys

import paramiko

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from run_closed_loop_activation_remote import execute, download_tree, save

PYTHON = "/home/lsh/.venvs/aeroweaver/bin/python"
REMOTE_ROOT = "/home/lsh/AeroWeaver"


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--calibration", action="store_true")
    mode.add_argument("--single-episode", action="store_true")
    mode.add_argument("--all-tasks-ablations", action="store_true")
    parser.add_argument("--seed", type=int, default=65001)
    parser.add_argument("--parent-output", help="Existing remote outputs directory; only zero-call episodes are continued")
    parser.add_argument("--model", choices=("deepseek-v4.1-flash-expires-on-0910", "deepseek-v4-flash", "deepseek-flash"), default="deepseek-v4.1-flash-expires-on-0910")
    parser.add_argument("--restart-incomplete", action="store_true", help="Explicitly authorize one fresh attempt for incomplete episodes")
    parser.add_argument("--allow-mixed-models", action="store_true")
    args = parser.parse_args()
    if args.parent_output and not args.all_tasks_ablations:
        parser.error("--parent-output requires --all-tasks-ablations")
    if (args.restart_incomplete or args.allow_mixed_models) and not args.parent_output:
        parser.error("Continuation switches require --parent-output")
    if args.model != "deepseek-v4.1-flash-expires-on-0910" and not args.all_tasks_ablations:
        parser.error("The model override currently requires --all-tasks-ablations")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    local = ROOT / "results/end-to-end-pilot" / stamp
    local.mkdir(parents=True, exist_ok=False)
    remote = "/home/lsh/.aeroweaver-experiments/end-to-end-pilot/" + stamp
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("10.61.3.7", username="lsh", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
    ssh.get_transport().set_keepalive(30)
    code, stage = None, "stage"
    try:
        status = "import json,urllib.request;s=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/status',timeout=10));v={k:s.get(k) for k in ['mission_active','is_executing','executing_robots']};print(json.dumps(v));assert not any(v.values()),'Live mission active'"
        if execute(ssh, shlex.join([PYTHON, "-c", status]), local / "status-before.log"):
            raise RuntimeError("Live app busy; no pilot started")
        _, out, err = ssh.exec_command("mkdir -p " + shlex.quote(remote + "/inputs"))
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read().decode())
        files = {
            "pilot.py": ROOT / "experiments/end_to_end_v1/pilot.py",
            "test_pilot.py": ROOT / "experiments/end_to_end_v1/test_pilot.py",
            "activation_support.py": ROOT / "experiments/skill_activation_closed_loop_v1/run.py",
            "catalog.json": ROOT / "experiments/skill_activation_v1/catalog.json",
            "EXPERIMENT_PLAN.md": ROOT / "experiments/end_to_end_v1/EXPERIMENT_PLAN.md",
        }
        if args.calibration:
            files.update({name: ROOT / "experiments/end_to_end_v1" / name for name in (
                "replay_calibration.py", "calibration_cases.json", "CALIBRATION_PROTOCOL.md")})
        if args.single_episode:
            files["SINGLE_EPISODE_PROTOCOL.md"] = ROOT / "experiments/end_to_end_v1/SINGLE_EPISODE_PROTOCOL.md"
        if args.all_tasks_ablations:
            files.update({name: ROOT / "experiments/end_to_end_v1" / name for name in (
                "run_all_tasks.py", "test_all_tasks.py", "ALL_TASKS_PROTOCOL.md")})
        with ssh.open_sftp() as sftp:
            for name, path in files.items():
                sftp.put(str(path), remote + "/inputs/" + name)
            bm25 = "/home/lsh/.venvs/aeroweaver-skill-eval/lib/python3.10/site-packages/rank_bm25.py"
            with sftp.open(bm25, "rb") as source, sftp.open(remote + "/inputs/rank_bm25.py", "wb") as target:
                target.write(source.read())
        prefix = ["env", "AEROWEAVER_REPO_ROOT=" + REMOTE_ROOT, PYTHON]
        commands = {"tests": shlex.join(prefix + ["-m", "pytest", "-q", remote + "/inputs/test_pilot.py", "--tb=short"])}
        if args.all_tasks_ablations:
            commands["all_task_tests"] = shlex.join(prefix + ["-m", "pytest", "-q", remote + "/inputs/test_all_tasks.py", "--tb=short"])
        if args.calibration:
            commands["calibration"] = shlex.join(["timeout", "600"] + prefix + ["-u", remote + "/inputs/replay_calibration.py",
                "--cases", remote + "/inputs/calibration_cases.json", "--catalog", remote + "/inputs/catalog.json",
                "--output", remote + "/calibration"])
        runner = "run_all_tasks.py" if args.all_tasks_ablations else "pilot.py"
        pilot_args = ["-u", remote + "/inputs/" + runner, "--catalog", remote + "/inputs/catalog.json",
                      "--output", remote + "/outputs"]
        if args.single_episode:
            pilot_args += ["--single-episode", "--seed", str(args.seed)]
        if args.all_tasks_ablations:
            pilot_args += ["--seed", str(args.seed), "--model", args.model]
            if args.parent_output:
                pilot_args += ["--parent-output", args.parent_output]
            if args.restart_incomplete:
                pilot_args += ["--restart-incomplete"]
            if args.allow_mixed_models:
                pilot_args += ["--allow-mixed-models"]
        commands["pilot"] = shlex.join(["timeout", "3700"] + prefix + pilot_args)
        save(local / "launch.json", {"remote": remote, "commands": commands, "host": "10.61.3.7"})
        print(f"Artifacts: {local}", flush=True)
        for stage, command in commands.items():
            save(local / "stage.json", {"stage": stage, "status": "running"})
            print("Stage: " + stage, flush=True)
            code = execute(ssh, command, local / (stage + ".log"))
            if code:
                print(f"Stage failed: {stage}, exit={code}. No automatic retry.", flush=True)
                break
        execute(ssh, shlex.join([PYTHON, "-c", status]), local / "status-after.log")
    finally:
        with ssh.open_sftp() as sftp:
            try:
                download_tree(sftp, remote, local / "remote-artifacts")
            except FileNotFoundError:
                pass
        ssh.close()
        save(local / "exit.json", {"stage": stage, "exit_code": code})
        print(f"Collected: {local}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
