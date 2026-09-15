"""Verify persisted reward trajectories through the running Mock HTTP service."""

import argparse
import json
import math
from pathlib import Path
import time
import urllib.request


BASE = "http://127.0.0.1:5001"


def request(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = []
    for scenario in ("coverage", "pursuit", "navigation", "private_communication", "circle", "line",
                     "world_communication", "collection", "concealment"):
        status = request("/api/status")
        assert not status["mission_active"] and not status["is_executing"], "Another task is active"
        payload = {"scenario_id": scenario, "policy": "local_skill_baseline", "max_rounds": 3,
                   "seed": 20260909, "experiment_id": "memory-recording-verification", "split": "verification"}
        if scenario == "pursuit":
            payload["role_assignments"] = [{"robot_id": f"UAV_{i}", "role": "pursuer" if i <= 6 else "evader"}
                                           for i in range(1, 9)]
        result = request("/api/mock/tasks/start", payload)
        mission = result["task"]["mission_id"]
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                current = request("/api/mock/tasks")["current"]
                assert current["mission_id"] == mission, "Task replaced during verification"
                if current["status"] != "running" and not request("/api/status")["mission_active"]:
                    break
                time.sleep(.2)
            else:
                raise TimeoutError("Verification task did not finish")
            assert current["status"] in {"complete", "horizon"}, current
            directory = Path("/home/runner/AeroWeaver/results/mock-tasks") / mission
            # The summary is the runner's final write after memory export and cleanup.
            for _ in range(50):
                if (directory / "summary.json").exists():
                    break
                time.sleep(.1)
            summary = json.loads((directory / "summary.json").read_text())
            rows = [json.loads(line) for line in (directory / "experience.jsonl").read_text().splitlines()]
            assert len(rows) == summary["experience_records"] == 3 * len(current["roles"])
            assert summary["invocation_errors"] == 0, summary
            for rid, role in current["roles"].items():
                trajectory = [r for r in rows if r["agent_id"] == rid]
                for i, row in enumerate(trajectory):
                    expected = sum(.95**j * r["immediate_reward"] for j, r in enumerate(trajectory[i:]))
                    assert math.isclose(expected, row["return_value"], rel_tol=1e-10, abs_tol=1e-10)
                    assert row["role"] == role and row["return_finalized"]
                    assert not row["metadata"]["reuse_allowed"] and not row["metadata"]["online_update"]
            cases.append({"scenario_id": scenario, "mission_id": mission, "records": len(rows),
                          "invocations": summary["invocations"], "reward_source": summary["reward_source"],
                          "state": current["status"], "path": str(directory)})
            print(json.dumps(cases[-1]), flush=True)
        finally:
            current = request("/api/mock/tasks")["current"]
            if current and current["mission_id"] == mission and current["status"] == "running":
                request("/api/mock/tasks/stop", {})
    report = {"verified": True, "scope": "recording integration smoke; not effectiveness experiments",
              "cases": cases, "stats": request("/api/memory/swarm-experience?limit=1")["stats"]}
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
