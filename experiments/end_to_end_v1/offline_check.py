"""Free environment/memory checks, explicitly NOT an end-to-end LLM baseline."""

import argparse
from collections import defaultdict
from copy import deepcopy
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import traceback


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def memory_checks(Memory, output):
    memory = Memory(output / "synthetic_unit_fixtures.sqlite3", gamma=.95, alpha=.8)
    state = {"position": [0, 0, -5], "role": "searcher"}
    checks = []
    try:
        def add(mission, role, skill, reward, step=0):
            return memory.record_step(mission_id=mission, task="coverage", state=state,
                next_state=state, action_id=f"UAV_1:{step}", skill=skill, agent_id="UAV_1",
                role=role, success=True, reward=reward, reward_source="synthetic_unit_fixture",
                step_index=step, metadata={"reuse_allowed": True, "split": "synthetic_unit_fixture"})

        add("discount", "searcher", "cover_landmark", 1, 0)
        assert memory.records("discount")[0].return_value == 1
        checks.append("partial_return_uses_only_observed_reward")
        add("discount", "searcher", "cover_landmark", 2, 1)
        records = memory.records("discount")
        assert math.isclose(records[0].return_value, 2.9)
        assert records[1].return_value == 2
        memory.finalize_mission(mission_id="discount", success=True)
        assert all(r.return_finalized for r in memory.records("discount"))
        checks.append("discount_and_episode_finalization")
        add("hold", "searcher", "hold_position", -4)
        add("opponent", "evader", "hold_position", 10000)
        candidates = [
            {"candidate_id": "cover", "skill": "cover_landmark", "base_logprob": -1.1},
            {"candidate_id": "hold", "skill": "hold_position", "base_logprob": -1.0},
        ]
        memory.alpha = 0
        base = memory.rank_candidates(task="coverage", state=state, role="searcher", candidates=candidates)
        assert base["selected"]["skill"] == "hold_position"
        checks.append("beta_zero_preserves_base_preference")
        memory.alpha = .8
        adjusted = memory.rank_candidates(task="coverage", state=state, role="searcher", candidates=candidates)
        assert adjusted["selected"]["skill"] == "cover_landmark"
        checks.append("reward_advantage_changes_preference")
        assert all(r.role == "searcher" for _, r in memory.retrieve(task="coverage", state=state, role="searcher"))
        checks.append("same_role_retrieval_excludes_opponent_fixture")
        unrelated = memory.rank_candidates(task="pursuit", state=state, role="searcher", candidates=candidates)
        assert unrelated["retrieved"] == 0 and unrelated["selected"]["skill"] == "hold_position"
        checks.append("unmatched_scenario_has_zero_correction")
        save(output / "memory_unit_checks.json", {"checks": checks, "base": base, "adjusted": adjusted,
             "scope": "synthetic fixtures only; not model logits, environment rewards, or performance evidence"})
    finally:
        memory.close()
    return checks


def run_episode(scenario, seed, output, Adapter, Task, Reward, Memory, policy):
    episode = f"offline-{scenario}-{seed}"
    directory = output / "episodes" / episode
    directory.mkdir(parents=True, exist_ok=False)
    task = Task(scenario, seed=seed, max_rounds=24)
    task.mission_id, task.policy, task.status = episode, "offline_rule_policy_check", "running"
    adapter = Adapter(realtime_factor=1)
    memory = Memory(output / "execution_trajectories.sqlite3", gamma=.95)
    returns = defaultdict(float)
    start = time.monotonic()
    try:
        adapter.connect()
        adapter.retain_fleet(task.roles)
        adapter.set_operating_bounds(task.bounds, list(task.roles))
        for rid in task.roles:
            adapter.reset_robot_pose(rid, task.positions[rid], in_air=True)
        task.sync(adapter.get_robot_snapshot())
        initial = deepcopy(task.positions)
        reward = Reward(task)
        save(directory / "initial_state.json", {"roles": task.roles, "positions": initial,
                                                 "objects": task.objects, "bounds": task.bounds})
        save(directory / "reward_manifest.json", reward.manifest)
        with (directory / "trace.jsonl").open("w", encoding="utf-8") as trace:
            while task.status == "running":
                if time.monotonic() - start > 60:
                    raise TimeoutError("Offline episode exceeded 60 seconds")
                step = task.round
                observations = {rid: task.observe(rid) for rid in task.roles}
                decisions = {rid: policy(obs) for rid, obs in observations.items()}
                offset = len(task.messages)
                for rid, choice in decisions.items():
                    task.apply(rid, choice["skill"], choice["parameters"], adapter)
                adapter.advance(task, round(.5 / adapter._dynamics.dt))
                task.evaluate()
                rewards = reward.rewards(task)
                next_obs = {rid: task.observe(rid) for rid in task.roles}
                for rid, choice in decisions.items():
                    assert math.isfinite(rewards[rid])
                    returns[rid] += rewards[rid]
                    memory.record_step(mission_id=episode, task=scenario, state=observations[rid],
                        next_state=next_obs[rid], action_id=f"{rid}:{step}", skill=choice["skill"],
                        agent_id=rid, role=task.roles[rid], step_index=step, success=True,
                        reward=rewards[rid], reward_source=reward.source,
                        invocation={"robot": rid, **choice},
                        metadata={"experiment_id": output.name, "split": "offline_engineering_check",
                                  "condition": "offline_rule_policy_check", "seed": seed,
                                  "reuse_allowed": False, "online_update": False, "native_mpe": False,
                                  "sim_interval_s": .5, "terminated": task.status == "complete",
                                  "truncated": task.status == "horizon"})
                trace.write(json.dumps({"step": step, "decisions": decisions, "rewards": rewards,
                                       "positions": task.positions, "messages": task.messages[offset:],
                                       "metrics": task.metrics}) + "\n")
        memory.finalize_mission(mission_id=episode, success=task.status == "complete")
        records = memory.records(episode)
        for rid in task.roles:
            trajectory = sorted((r for r in records if r.agent_id == rid), key=lambda r: r.step_index)
            expected = 0.0
            for record in reversed(trajectory):
                expected = record.immediate_reward + .95 * expected
                assert math.isclose(record.return_value, expected, rel_tol=1e-10, abs_tol=1e-10)
                assert record.return_finalized
            assert [r.step_index for r in trajectory] == list(range(task.round))
        assert len(records) == len(task.roles) * task.round
        assert not memory.retrieve(task=scenario, state={}, role=next(iter(task.roles.values())))
        memory.export_mission(episode, directory / "experience.jsonl")
        displacement = {rid: math.dist(initial[rid], task.positions[rid]) for rid in task.roles}
        assert max(displacement.values()) > 0
        summary = {"episode_id": episode, "scenario": scenario, "seed": seed, "status": task.status,
                   "policy": "offline_rule_policy_check", "llm_calls": 0, "rounds": task.round,
                   "records": len(records), "discount_checks_passed": len(records),
                   "role_counts": {role: list(task.roles.values()).count(role) for role in set(task.roles.values())},
                   "returns_by_agent": dict(returns), "peer_messages": len(task.messages),
                   "displacement_m": displacement, "seconds": time.monotonic() - start,
                   "scope": "environment and recorder smoke test, not an AeroWeaver or baseline result"}
        save(directory / "summary.json", summary)
        return summary
    except Exception:
        memory.export_mission(episode, directory / "experience_partial.jsonl")
        save(directory / "failure.json", {"episode_id": episode, "status": "technical_failure",
             "round": task.round, "records": len(memory.records(episode)),
             "traceback": traceback.format_exc(), "automatically_retried": False})
        raise
    finally:
        memory.close()
        adapter.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/runner/AeroWeaver"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenarios", nargs="+", choices=("coverage", "pursuit", "world_communication"),
                        default=["coverage", "pursuit", "world_communication"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.root / "backend"))
    from adapters.mock_adapter import MockAdapter
    from memory.swarm_experience import SwarmExperienceMemory
    from sim.mock_tasks import MockTask, local_skill_choice
    from sim.mock_rewards import MockMPEReward

    class FixedStepAdapter(MockAdapter):
        def _ensure_physics_thread(self):
            return

        def advance(self, task, ticks):
            for _ in range(ticks):
                task.refresh_motion(self)
                with self._state_lock:
                    self._step_world_locked()
            task.sync(self.get_robot_snapshot())

    files = ["backend/adapters/mock_adapter.py", "backend/adapters/mock_dynamics.py",
             "backend/sim/mock_tasks.py", "backend/sim/mock_rewards.py", "backend/memory/swarm_experience.py"]
    sources = {name: hashlib.sha256((args.root / name).read_bytes()).hexdigest() for name in files}
    planned = len(args.scenarios) * 2
    save(args.output / "manifest.json", {"scenarios": args.scenarios,
         "seeds": [61011, 61012], "rounds": 24, "planned_episodes": planned, "llm_calls": 0,
         "source_hashes": sources, "gamma": .95, "physics_dt_s": .05,
         "decision_interval_s": .5, "scope": "offline diagnostics only; no trained baselines",
         "production_memory_modified": False})
    checks = memory_checks(SwarmExperienceMemory, args.output)
    rows = []
    for scenario in args.scenarios:
        for seed in (61011, 61012):
            row = run_episode(scenario, seed, args.output, FixedStepAdapter, MockTask,
                              MockMPEReward, SwarmExperienceMemory, local_skill_choice)
            rows.append(row)
            save(args.output / "progress.json", {"completed": len(rows), "planned": planned})
            print(json.dumps({"episode": row["episode_id"], "status": row["status"],
                              "records": row["records"]}), flush=True)
    assert all(hashlib.sha256((args.root / name).read_bytes()).hexdigest() == value for name, value in sources.items())
    save(args.output / "verification.json", {"status": "passed", "episodes": len(rows),
         "records": sum(r["records"] for r in rows), "unit_checks": checks,
         "source_hashes_unchanged": True, "main_experiment_run": False,
         "model_gate": "blocked_by_provider_HTTP_402", "episodes_detail": rows})
    with (args.output / "episode_checks.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = ["episode_id", "scenario", "seed", "policy", "status", "rounds", "records", "peer_messages", "seconds"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"offline_check": "passed", "episodes": len(rows),
                      "records": sum(r["records"] for r in rows), "unit_checks": len(checks)}))


if __name__ == "__main__":
    main()
