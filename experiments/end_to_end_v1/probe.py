"""Read-only remote readiness check and one bounded provider capability probe."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import sys
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/lsh/AeroWeaver"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider-probe", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.root / "backend"))
    with urllib.request.urlopen("http://127.0.0.1:5001/api/status", timeout=10) as response:
        status = json.load(response)
    report = {
        "python": sys.version, "platform": platform.platform(),
        "live_status": {k: status.get(k) for k in ("mission_active", "is_executing", "executing_robots")},
        "packages": {name: importlib.util.find_spec(name) is not None
                     for name in ("torch", "mpe2", "pytest", "rank_bm25")},
        "sources": {},
    }
    for name in ("backend/sim/mock_task_api.py", "backend/sim/mock_tasks.py",
                 "backend/memory/swarm_experience.py", "backend/llm_client.py"):
        report["sources"][name] = hashlib.sha256((args.root / name).read_bytes()).hexdigest()
    if args.provider_probe:
        assert not any(report["live_status"].values()), "Live mission active; skip API probe"
        from llm_client import get_client
        from sim.mock_tasks import MockTask
        client = get_client(module="planner")
        assert client.model == "deepseek-v4.1-flash-expires-on-0910", "Unexpected model"
        assert client._base_url.rstrip("/") in ("https://api.deepseek.com", "https://api.deepseek.com/v1")
        task = MockTask("coverage", seed=61001)
        obs = task.observe(next(iter(task.roles)))
        obs.pop("agent_system_prompt", None)
        options = obs.pop("options")
        labels = [chr(65 + i) for i in range(len(options))]
        payload = {
            "model": client.model, "stream": False, "thinking": {"type": "disabled"},
            "temperature": 1, "max_tokens": 4, "logprobs": True, "top_logprobs": 20,
            "messages": [{"role": "system", "content": "Select the best local action for cooperative coverage. Output exactly one ASCII option letter, without whitespace, punctuation or explanation."},
                         {"role": "user", "content": json.dumps({"observation": obs, "options": dict(zip(labels, options))})}],
        }
        request = urllib.request.Request(client._base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + client._api_key})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                body = json.load(response)
            report["provider"] = {"model": body.get("model"), "usage": body.get("usage"),
                                  "labels": labels, "choices": body.get("choices")}
        except Exception as exc:
            report["provider"] = {"error_type": type(exc).__name__, "error": str(exc)}
    (args.output / "probe.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
