"""Run exactly one archived-protocol World Communication HMAS-2 episode."""

import argparse
from functools import partial
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-inputs", type=Path, required=True)
    parser.add_argument("--reference-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.pilot_inputs))
    import pilot
    import run_all_tasks as suite

    suite.configure_support()
    scenario, method, seed = "world_communication", "hmas2_adapted", 66001
    old_id = f"{scenario}-{method}-single_episode-{seed}"
    reference = json.loads((args.reference_output / "episodes" / old_id / "summary.json").read_text())
    skills = json.loads((args.pilot_inputs / "catalog.json").read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    source_hashes = {name: pilot.support.sha(pilot.support.ROOT / name)
                     for name in pilot.support.SOURCE_FILES}
    old_plan = json.loads((args.reference_output / "plan.json").read_text())
    plan = {"planned_episodes": 1, "scenario": scenario, "method": method, "seed": seed,
            "rounds": 24, "model": reference["model"], "retries": 0,
            "max_calls": 120, "max_observed_tokens": 500000,
            "reference_episode": old_id, "reference_return": reference["controlled_team_mean_return"],
            "source_hashes": source_hashes,
            "source_changes_since_reference": [name for name, value in source_hashes.items()
                                               if old_plan["source_hashes"].get(name) != value],
            "pilot_sha256": pilot.support.sha(Path(pilot.__file__)),
            "runner_sha256": pilot.support.sha(Path(suite.__file__)),
            "catalog_sha256": pilot.support.sha(args.pilot_inputs / "catalog.json"),
            "memory": "fresh isolated SQLite; one episode only"}
    pilot.support.dump(args.output / "plan.json", plan)
    client = pilot.Client(args.output / "calls.jsonl", call_limit=120, token_limit=500000,
                          max_inflight=3, model=reference["model"])
    journal_class = pilot.support.Journal

    class ProgressJournal(journal_class):
        def append(self, value):
            super().append(value)
            if self.path.name == "trace.jsonl":
                print(json.dumps({"completed_steps": value["step"] + 1,
                                  "planned_steps": 24,
                                  "controlled_rewards": {rid: value["rewards"][rid]
                                                         for rid in ("UAV_1", "UAV_2", "UAV_3")},
                                  "pursuit_contacts": value["metrics"]["pursuit_contacts"]}), flush=True)

    pilot.support.Journal = ProgressJournal
    try:
        row = pilot.run_episode(scenario, method, seed, "single_recheck", args.output,
                                skills, client, args.output / "memory" / "hmas2.sqlite3",
                                rounds=24, active_override=[s["name"] for s in skills],
                                task_factory=partial(suite.ExperimentTask, condition=method))
    finally:
        pilot.support.Journal = journal_class
    row.update(calls=client.calls, tokens=client.tokens, accounted_calls=client.calls,
               accounted_tokens=client.tokens, activation_setup_calls=0, activation_setup_tokens=0)
    episode_dir = args.output / "episodes" / row["episode_id"]
    pilot.support.dump(episode_dir / "summary.json", row)
    comparison = {"reference_return": reference["controlled_team_mean_return"],
                  "recheck_return": row.get("controlled_team_mean_return"),
                  "same_initial_state": row.get("initial_state_sha256") == reference["initial_state_sha256"],
                  "same_reward_manifest": json.loads((episode_dir / "reward_manifest.json").read_text()) ==
                      json.loads((args.reference_output / "episodes" / old_id / "reward_manifest.json").read_text()),
                  "source_hashes_unchanged_during_run": all(pilot.support.sha(pilot.support.ROOT / name) == value
                                                          for name, value in source_hashes.items()),
                  "source_changes_since_reference": plan["source_changes_since_reference"],
                  "planned_episodes": 1, "attempted_episodes": 1, "summary": row}
    pilot.support.dump(args.output / "comparison.json", comparison)
    print(json.dumps(comparison), flush=True)
    return int(row["status"] not in {"complete", "horizon"})


if __name__ == "__main__":
    raise SystemExit(main())
