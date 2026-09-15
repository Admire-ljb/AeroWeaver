"""Small, paired Mock rollouts with write-only trajectory collection."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import urllib.request


ROOT = Path("/home/runner/AeroWeaver")
BASE = "http://127.0.0.1:5001"
SCENARIOS = ("coverage", "pursuit", "world_communication")
POLICIES = ("local_skill_baseline", "llm")


def request(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_episode(mission, payload):
    directory = ROOT / "results/mock-tasks" / mission
    summary = json.loads((directory / "summary.json").read_text())
    records = [json.loads(line) for line in (directory / "experience.jsonl").read_text().splitlines()]
    traces = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    grouped = defaultdict(list)
    assert records, "No executed trajectories were persisted"
    for record in records:
        assert record["mission_id"] == mission
        assert record["role"] == summary["roles"][record["agent_id"]]
        assert record["next_state"] is not None and record["invocation"]
        assert record["reward_source"] == summary["reward_source"]
        assert record["return_finalized"]
        assert record["metadata"]["experiment_id"] == payload["experiment_id"]
        assert record["metadata"]["split"] == payload["split"]
        assert record["gamma"] == .95
        assert not record["metadata"]["reuse_allowed"] and not record["metadata"]["online_update"]
        grouped[(record["agent_id"], record["trajectory_id"])].append(record)
    for rows in grouped.values():
        rows.sort(key=lambda row: row["step_index"])
        for i, row in enumerate(rows):
            expected = sum(row["gamma"] ** (r["step_index"] - row["step_index"]) * r["immediate_reward"]
                           for r in rows[i:])
            assert math.isfinite(expected) and math.isclose(expected, row["return_value"], rel_tol=1e-10, abs_tol=1e-10)
    path = Path(summary["memory_path"]).resolve()
    assert path.is_relative_to(ROOT / "backend/data"), "Unexpected memory database"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        stored = {row[0]: row for row in db.execute(
            "SELECT id,reward,return_value,finalized FROM transitions WHERE mission=?", (mission,))}
    assert len(stored) == len(records) == summary["experience_records"]
    for row in records:
        persisted = stored[row["experience_id"]]
        assert persisted[1:] == (row["immediate_reward"], row["return_value"], 1)
    selected = [item for row in traces for item in row.get("decisions", []) if item.get("choice")]
    missing = sorted(set(summary["roles"]) - {key[0] for key in grouped})
    return {
        "mission_id": mission, "scenario_id": summary["task_id"], "policy": summary["policy"],
        "status": summary["status"], "rounds": summary["round"], "records": len(records),
        "records_by_role": dict(Counter(row["role"] for row in records)),
        "missing_participant_trajectories": missing, "invocations": summary["invocations"],
        "invocation_errors": summary["invocation_errors"], "llm_calls_started": summary["llm_calls"],
        "fallback_invocations": sum(item["selector_source"] == "local_fallback" for item in selected),
        "selector_sources": dict(Counter(item["selector_source"] for item in selected)),
        "peer_messages": sum(len(row.get("messages", [])) for row in traces),
        "undiscounted_return_by_agent": {rid: sum(row["immediate_reward"] for row in rows)
                                         for (rid, segment), rows in grouped.items()},
        "diagnostics": summary["metrics"], "duration_s": summary["duration_s"],
        "reward_source": summary["reward_source"], "path": str(directory),
        "hashes": {name: sha(directory / name) for name in
                   ("experience.jsonl", "trace.jsonl", "summary.json", "reward_manifest.json")},
        "memory_verified": True,
    }


def run_episode(payload, report):
    status = request("/api/status")
    if status["mission_active"] or status["is_executing"] or status["executing_robots"]:
        raise RuntimeError("Another mission is active; nothing interrupted")
    start = request("/api/mock/tasks/start", payload)
    mission = start["task"]["mission_id"]
    report["started_missions"].append({"mission_id": mission, "payload": payload})
    print(json.dumps({"started": mission, **payload}), flush=True)
    try:
        deadline = time.monotonic() + 90
        next_notice = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = request("/api/mock/tasks")["current"]
            if not current or current["mission_id"] != mission:
                raise RuntimeError("Task replaced externally; stop batch without changing the replacement")
            summary_path = ROOT / "results/mock-tasks" / mission / "summary.json"
            if current["status"] != "running" and summary_path.exists():
                return inspect_episode(mission, payload)
            if time.monotonic() >= next_notice:
                print(json.dumps({"running": mission, "round": current["round"], "status": current["status"]}), flush=True)
                next_notice = time.monotonic() + 15
            time.sleep(.25)
        raise TimeoutError("Episode exceeded the fixed 90-second budget")
    finally:
        current = request("/api/mock/tasks")["current"]
        if current and current["mission_id"] == mission and current["status"] == "running":
            request("/api/mock/tasks/stop", {})
            for _ in range(100):
                if (ROOT / "results/mock-tasks" / mission / "summary.json").exists():
                    break
                time.sleep(.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    experiment_id = "memory-collection-" + args.output.parent.name
    before = request("/api/memory/swarm-experience?limit=1")["stats"]
    plan = {"created_at": datetime.now(timezone.utc).isoformat(), "experiment_id": experiment_id,
            "scenarios": SCENARIOS, "policies": POLICIES, "seed": 17, "max_rounds": 24,
            "episode_timeout_s": 90, "gamma": .95, "experience_reuse": False, "online_update": False,
            "split": "development_collection", "scope": "small Mock trajectory collection, not a formal effectiveness comparison",
            "reward_scope": "MPE2 scenario reward functions on Mock state, not native MPE rollouts",
            "baseline": "existing deterministic local option selector",
            "llm_condition": "existing planner channel, role-local prompts, static active role skills; temperature 0.2",
            "fallback_policy": "retain and report existing runtime fallbacks; never discard failed invocations",
            "backend_hashes": {name: sha(ROOT / name) for name in (
                "backend/sim/mock_tasks.py", "backend/sim/mock_task_api.py", "backend/sim/mock_rewards.py",
                "backend/memory/swarm_experience.py", "backend/memory/mock_trajectory.py", "backend/llm_client.py")},
            "runner_sha256": sha(Path(__file__)), "memory_before": before}
    dump(args.output / "plan.json", plan)
    report = {"experiment_id": experiment_id, "status": "running", "started_missions": [], "episodes": []}
    try:
        for scenario in SCENARIOS:
            for policy in POLICIES:
                payload = {"scenario_id": scenario, "policy": policy, "seed": 17, "max_rounds": 24,
                           "experiment_id": experiment_id, "split": plan["split"]}
                result = run_episode(payload, report)
                report["episodes"].append(result)
                dump(args.output / "progress.json", report)
                print(json.dumps(result), flush=True)
                if result["status"] not in {"complete", "horizon"}:
                    raise RuntimeError("Runtime ended abnormally; retain results and stop the batch")
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["memory_after"] = request("/api/memory/swarm-experience?limit=1")["stats"]
        report["new_records"] = sum(row["records"] for row in report["episodes"])
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        dump(args.output / "report.json", report)
        print(json.dumps({key: report[key] for key in ("status", "new_records", "memory_after")}), flush=True)


if __name__ == "__main__":
    main()
