"""Bounded engineering smoke runs through the already deployed Mock console."""

import argparse
import json
from pathlib import Path
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5001")
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--output", default="results/mock-console-smoke.json")
    args = parser.parse_args()
    def call(path, data=None):
        payload = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(args.url + path, payload, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    if call("/api/status")["mission_active"]:
        raise RuntimeError("Refusing to interrupt an active user mission")
    reports = []
    for spec in call("/api/mock/tasks")["tasks"]:
        response = call("/api/mock/tasks/start", {"task_id": spec["id"], "seed": 7,
                        "max_rounds": args.rounds, "policy": "local_skill_baseline"})
        assert response["ok"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            task = call("/api/mock/tasks")["current"]
            system = call("/api/status")
            if task["status"] != "running" and not system["executing_robots"]:
                break
            time.sleep(0.15)
        else:
            call("/api/mock/tasks/stop", {})
            raise TimeoutError(spec["id"])
        assert task["status"] in {"complete", "horizon"}, task
        reports.append(task)
        print(json.dumps({"task": spec["id"], "status": task["status"], "rounds": task["round"]}), flush=True)
    call("/api/mode", {"mode": "manual"})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"scope": "Mock console engineering smoke test; no LLM calls or RL results",
                                  "tasks": reports}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
