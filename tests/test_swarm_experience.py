import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory.swarm_experience import SkillCandidate, SwarmExperienceMemory


def add(memory, reward, *, agent="UAV_1", mission="mission", step=None, skill="pursue_target",
        role="pursuer", reuse=False, source="mpe2:1.1.0:simple_tag:mock_state_v1", **kwargs):
    return memory.record_step(
        mission_id=mission, task="pursuit", state={"position": [0, 0], "role": role},
        action_id=skill, skill=skill, agent_id=agent, role=role, success=True,
        reward=reward, reward_source=source, step_index=step,
        invocation={"skill": skill, "parameters": {"target_id": "UAV_4"}},
        next_state={"position": [1, 0]}, metadata={"reuse_allowed": reuse}, **kwargs)


def test_zero_reward_and_unclipped_environment_rewards(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    for r in (0, 10, -20):
        add(memory, r)
    records = memory.records()
    assert [r.immediate_reward for r in records] == [0, 10, -20]
    assert [r.return_value for r in records] == pytest.approx([0 + .95 * 10 - .95**2 * 20, 10 - .95 * 20, -20])
    memory.close()


def test_returns_never_cross_agents_or_missions(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    add(memory, 1, agent="UAV_1")
    add(memory, 100, agent="UAV_2")
    add(memory, 2, agent="UAV_1")
    add(memory, -100, mission="another")
    assert [r.return_value for r in memory.records()] == pytest.approx([2.9, 100, 2, -100])
    memory.close()


def test_returns_extend_only_with_observed_rewards_and_persist(tmp_path):
    path = tmp_path / "memory.sqlite3"
    memory = SwarmExperienceMemory(path)
    add(memory, 1)
    assert memory.records()[0].return_value == 1
    add(memory, 2)
    assert memory.records()[0].return_value == 2.9
    memory.finalize_mission(mission_id="mission", success=False, votes={"UAV_1": False},
                            trace_summary={"quality": -100}, overall_reward=-999)
    memory.close()
    restored = SwarmExperienceMemory(path)
    assert [r.return_value for r in restored.records()] == [2.9, 2]
    assert all(r.return_finalized for r in restored.records())
    restored.close()


def test_non_execution_events_do_not_enter_memory(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    memory.begin_mission("mission", "pursuit", ["UAV_1"])
    memory.record_agent_result(mission_id="mission", agent_id="UAV_1", success=True, task="pursuit")
    memory.record_trace("mission", {"quality": 1})
    memory.record_vote(mission_id="mission", agent_id="UAV_1", ready=True)
    assert memory.records() == []
    assert memory.events() == []
    memory.close()


def test_unknown_reward_is_not_inferred_from_success_or_old_reward_arguments(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    add(memory, 123, source=None, reuse=True, trace={"quality": 1, "progress": 1})
    record = memory.records()[0]
    assert record.immediate_reward is None and record.return_value is None
    assert memory.retrieve(task="pursuit", state=record.state, role="pursuer") == []
    memory.close()


def test_only_same_role_same_task_opted_in_records_can_change_scores(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    add(memory, 10, skill="pursue_target", agent="UAV_1", reuse=True)
    add(memory, 0, skill="follow_peer", agent="UAV_2", reuse=True)
    add(memory, 1000, skill="follow_peer", agent="UAV_3", reuse=False)
    add(memory, 1000, skill="follow_peer", agent="UAV_4", role="evader", reuse=True)
    policy = memory.rank_candidates(task="pursuit", state="local position", role="pursuer",
        candidates=[SkillCandidate("a", "pursue_target", "UAV_5"),
                    SkillCandidate("b", "follow_peer", "UAV_5")])
    assert policy["selected"]["skill"] == "pursue_target"
    assert policy["retrieved"] == 2
    assert memory.retrieve(task="another task", state="local position", role="pursuer") == []
    memory.close()


def test_legacy_jsonl_is_preserved_not_imported(tmp_path):
    path = tmp_path / "experience.jsonl"
    path.write_text('{"immediate_reward": 1, "source": "synthetic"}\n')
    memory = SwarmExperienceMemory(path)
    assert memory.path.suffix == ".sqlite3"
    assert memory.records() == []
    assert "synthetic" in path.read_text()
    memory.close()


def test_out_of_order_steps_finalized_append_and_nonfinite_reward_rejected(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    add(memory, 1, step=3)
    with pytest.raises(ValueError, match="increasing"):
        add(memory, 2, step=2)
    with pytest.raises(ValueError, match="finite"):
        add(memory, float("nan"))
    memory.finalize_mission(mission_id="mission", success=True)
    with pytest.raises(ValueError, match="finalized"):
        add(memory, 1)
    memory.close()


def test_trajectory_segments_discount_gaps_and_read_capacity(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3", max_records=2)
    add(memory, 1, step=0)
    add(memory, 2, step=2)
    add(memory, 20, trajectory_id="restart")
    all_records = memory.records("mission")
    assert len(all_records) == 3
    assert all_records[0].return_value == pytest.approx(1 + .95**2 * 2)
    assert len(memory.records()) == 2
    path = tmp_path / "export.jsonl"
    memory.export_mission("mission", path)
    assert len(path.read_text().splitlines()) == 3
    memory.close()


def test_concurrent_agents_keep_complete_trajectories(tmp_path):
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    def run(agent):
        for _ in range(20):
            add(memory, 1, agent=agent)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(run, ["UAV_1", "UAV_2", "UAV_3", "UAV_4"]))
    assert memory.stats()["records"] == 80
    assert sum(r.return_value for r in memory.records() if r.step_index == 0) == pytest.approx(4 * sum(.95**i for i in range(20)))
    memory.close()
