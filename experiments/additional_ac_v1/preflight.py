"""Read runtime provenance and test the requested model without a task rollout."""

import argparse
import json
from pathlib import Path
import sys
import traceback
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot-inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.pilot_inputs))
    import pilot
    import run_all_tasks as suite

    suite.configure_support()
    support = pilot.support
    status = json.load(urllib.request.urlopen("http://127.0.0.1:5001/api/status", timeout=10))
    flags = {k: status.get(k) for k in ("mission_active", "is_executing", "executing_robots", "ai_executing")}
    support.dump(args.output / "live-status.json", flags)
    assert not any(flags.values()), "Live mission active; do not launch experiments"
    support.dump(args.output / "provenance.json", {
        "requested_model": "deepseek-v4-flash",
        "source_hashes": {n: support.sha(support.ROOT / n) for n in support.SOURCE_FILES},
        "input_hashes": {p.name: support.sha(p) for p in args.pilot_inputs.glob("*.py")},
        "catalog_sha256": support.sha(args.pilot_inputs / "catalog.json"),
        "task_texts": {s: support.TASK_TEXT[s] for s in ("world_communication", "collection")},
        "purpose": "Interface check only; no reward or task result is generated",
    })
    client = pilot.Client(args.output / "calls.jsonl", call_limit=2, token_limit=4000,
                          max_inflight=1, model="deepseek-v4-flash")
    result = {"status": "failed", "episodes_started": 0}
    try:
        suite.check_model_interface(client, args.output)
        result["status"] = "passed"
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
    finally:
        result.update(calls=client.calls, tokens=client.tokens)
        support.dump(args.output / "result.json", result)
        print(json.dumps(result), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
