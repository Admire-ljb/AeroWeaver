"""Recover a finished rollout's export from immutable SQLite and trace evidence.

No simulation, inference, database writes, or reward imputation is performed.
"""

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import statistics


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    output = args.run / "remote-artifacts/outputs"
    plan = load(output / "plan.json")
    assert not (output / "completion.json").exists(), "Refuse to replace an existing completed summary"
    original = args.run / "recovery-originals"
    original.mkdir(exist_ok=False)
    folders = sorted((output / "episodes").iterdir())
    assert len(folders) == 54
    missing = [p for p in folders if not (p / "summary.json").exists()]
    assert len(missing) == 1 and missing[0].name == "collection-aeroweaver_full_catalog-single_episode-66001"
    directory = missing[0]
    episode = directory.name
    initial = load(directory / "initial_state.json")
    events = [json.loads(line) for line in (directory / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["step"] for e in events] == list(range(plan["rounds"]))
    database = output / "memory/collection-aeroweaver_full_catalog.sqlite3"
    database_hash = sha(database)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        stored = db.execute("SELECT * FROM transitions ORDER BY rowid").fetchall()
    assert len(stored) == len(initial["roles"]) * plan["rounds"] == 96
    records = []
    for item in stored:
        assert item["mission"] == episode and item["finalized"] == 1
        record = json.loads(item["payload"])
        assert record["immediate_reward"] == item["reward"]
        assert events[item["step"]]["rewards"][item["agent"]] == item["reward"]
        assert events[item["step"]]["decisions"][item["agent"]]["choice"]["skill"] == record["skill"]
        record.update(return_value=item["return_value"], return_finalized=True)
        records.append(record)
    last = [r for r in records if r["step_index"] == plan["rounds"] - 1]
    assert all(r["metadata"]["truncated"] and not r["metadata"]["terminated"] for r in last)
    decisions = [d for event in events for d in event["decisions"].values()]
    rankings = [d["ranking"] for d in decisions if d.get("ranking")]
    returns = {agent: sum(e["rewards"][agent] for e in events) for agent in initial["roles"]}
    row = {"episode_id": episode, "scenario": "collection", "method": "aeroweaver_full_catalog",
           "split": "single_episode", "seed": plan["seeds"][0], "model": plan["model"],
           "status": "horizon", "rounds": len(events), "records": len(records),
           "roles": initial["roles"], "returns_by_agent": returns,
           "controlled_team_mean_return": statistics.mean(returns.values()), "task_success": False,
           "active_skills": load(directory / "activation.json")["active"],
           "initial_state_sha256": hashlib.sha256(json.dumps(initial, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
           "final_metrics": events[-1]["metrics"],
           "p50_decision_latency_s": statistics.median(e["decision_latency_s"] for e in events),
           "decision_errors": sum(bool(d.get("error")) and "attempted_choice" not in d for d in decisions),
           "invocation_errors": sum("attempted_choice" in d for d in decisions),
           "review_errors": sum(bool((d.get("local_review") or {}).get("review_error")) for d in decisions),
           "ranking_decisions": len(rankings), "memory_ranking_changes": sum(r["changed_by_memory"] for r in rankings),
           "retrieval_decisions": sum(bool(r["retrieval_count"]) for r in rankings),
           "uncertified_rankings": sum(not r["certified"] for r in rankings),
           "peer_messages": sum(len(e["messages"]) for e in events), "suppressed_coordination_messages": 0,
           "elapsed_s": None, "interface_version": plan["interface_version"],
           "summary_recovered_from": "finalized SQLite records and complete 24-step trace; no episode rerun",
           "artifact_export_error": "dataclasses.asdict raised TypeError after mission finalization"}
    shutil.copy2(directory / "experience.jsonl", original / "partial-experience.jsonl")
    with (directory / "experience.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    assert sha(database) == database_hash
    dump(directory / "summary.json", row)
    usage = defaultdict(lambda: {"calls": 0, "tokens": 0})
    models = defaultdict(set)
    calls = [json.loads(line) for line in (output / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    for call in calls:
        usage[call["episode_id"]]["calls"] += 1
        usage[call["episode_id"]]["tokens"] += call.get("usage", {}).get("total_tokens", 0)
        models[call["episode_id"]].add(call["request"]["model"])
    restarts = {r["episode_id"]: r["restarted_from"] for r in load(output / "restart_manifest.json")["attempts"]}
    rows = []
    for folder in folders:
        path = folder / "summary.json"
        summary = load(path)
        shutil.copy2(path, original / (folder.name + "-summary.json"))
        assert summary["status"] in {"complete", "horizon"}
        assert len(models[summary["episode_id"]]) == 1
        summary["model"] = next(iter(models[summary["episode_id"]]))
        summary.update(usage[summary["episode_id"]])
        active = summary["method"] in {"aeroweaver_pilot", "aeroweaver_no_peer", "aeroweaver_no_rl"}
        setup = usage["activation-" + summary["scenario"]] if active else {"calls": 0, "tokens": 0}
        summary.update(activation_setup_calls=setup["calls"], activation_setup_tokens=setup["tokens"],
                       accounted_calls=summary["calls"] + setup["calls"], accounted_tokens=summary["tokens"] + setup["tokens"])
        if summary["episode_id"] in restarts:
            summary["restarted_from"] = restarts[summary["episode_id"]]
        dump(path, summary)
        rows.append(summary)
    fields = ["episode_id", "scenario", "method", "split", "seed", "model", "restarted_from", "status", "rounds", "records",
              "controlled_team_mean_return", "task_success", "calls", "tokens", "activation_setup_calls",
              "activation_setup_tokens", "accounted_calls", "accounted_tokens", "p50_decision_latency_s",
              "decision_errors", "invocation_errors", "review_errors", "retrieval_decisions", "ranking_decisions",
              "memory_ranking_changes", "uncertified_rankings", "peer_messages", "suppressed_coordination_messages", "elapsed_s",
              "artifact_export_error", "summary_recovered_from"]
    with (output / "episode_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    dump(output / "completion.json", {"completed_episodes": 54, "finished_rollouts": 54, "planned_episodes": 54,
         "technical_failures": 0, "artifact_export_failures": 1, "activation_errors": {}, "provider_stop": False,
         "calls": len(calls), "tokens": sum(c.get("usage", {}).get("total_tokens", 0) for c in calls),
         "source_hashes_unchanged": "not_checked_after_export_exception", "newly_executed_episodes": 40,
         "max_inflight_requests": 3, "main_experiment_complete": False, "llm_pilot_finished": True,
         "summary_recovery": "Offline aggregation after export failure; original process exit remains 1"})
    dump(args.run / "recovery-manifest.json", {"recovered_episode": episode, "trace_steps": len(events),
         "finalized_database_records": len(records), "database_sha256_unchanged": database_hash,
         "summary_return": row["controlled_team_mean_return"], "original_process_exit": load(args.run / "exit.json"),
         "new_inference_calls": 0, "episode_reruns": 0, "original_exports": "recovery-originals",
         "elapsed_time_recovered": False})
    print(json.dumps({"completed_rollouts": len(rows), "recovered": row}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
