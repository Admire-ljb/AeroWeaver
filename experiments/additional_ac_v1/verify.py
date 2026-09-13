"""Verify collected artifacts against remote hashes and episode/call ledgers."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shlex

import paramiko


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    launch = json.loads((args.run_directory / "launch.json").read_text(encoding="utf-8"))
    root = args.run_directory / "remote-artifacts/outputs"
    remote = launch["remote"] + "/outputs"
    code = (
        "import json,hashlib; from pathlib import Path; p=Path(" + repr(remote) + "); "
        "files=[p/'plan.json',p/'episode_metrics.csv',p/'aggregate_metrics.csv',p/'completion.json',"
        "*p.glob('calls/*.jsonl'),*p.glob('episodes/*/summary.json'),*p.glob('memory/*.sqlite3')]; "
        "print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}))"
    )
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("10.61.3.7", username="lsh", password=os.environ["AEROWEAVER_DEPLOY_PASSWORD"], timeout=15)
        _, out, err = ssh.exec_command(shlex.join(["/home/lsh/.venvs/aeroweaver/bin/python", "-c", code]))
        hashes = json.loads(out.read())
        assert out.channel.recv_exit_status() == 0, err.read().decode()
    finally:
        ssh.close()
    mismatches = [name for name, digest in hashes.items() if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest]
    assert not mismatches, mismatches
    with (root / "episode_metrics.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len({r["episode_id"] for r in rows})
    ledger_tokens = ledger_calls = 0
    for row in rows:
        summary = json.loads((root / "episodes" / row["episode_id"] / "summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == row["status"]
        if row["status"] == "technical_failure":
            assert row["controlled_team_mean_return"] == ""
        else:
            assert summary["controlled_team_mean_return"] == float(row["controlled_team_mean_return"])
        calls_path = root / "calls" / (row["episode_id"] + ".jsonl")
        calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()] if calls_path.exists() else []
        tokens = sum(c.get("usage", {}).get("total_tokens", 0) for c in calls)
        assert tokens == int(row["tokens"]) == summary["tokens"]
        assert len(calls) == int(row["calls"]) == summary["calls"]
        ledger_tokens += tokens
        ledger_calls += len(calls)
    result = dict(files_verified=len(hashes), mismatches=mismatches, episode_rows=len(rows),
                  episode_calls=ledger_calls, episode_tokens=ledger_tokens,
                  note="Episode totals exclude shared setup; journals include usage from incomplete responses.",
                  remote_sha256=hashes)
    (root / "download_verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "remote_sha256"}))


if __name__ == "__main__":
    main()
