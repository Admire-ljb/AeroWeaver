import math
from types import SimpleNamespace

import pytest

from sim.mock_tasks import MockTask, TASKS, local_skill_choice
from sim.mock_rewards import MockMPEReward
from memory.mock_trajectory import MockTrajectoryRecorder
from memory.swarm_experience import SwarmExperienceMemory


@pytest.mark.parametrize("task_id", TASKS)
def test_nine_scenarios_supply_one_finite_reward_per_actual_role(task_id):
    task = MockTask(task_id)
    model = MockMPEReward(task)
    rewards = model.rewards(task)
    assert rewards.keys() == task.roles.keys()
    assert all(math.isfinite(r) for r in rewards.values())
    assert model.manifest["native_mpe_rollout"] is False
    assert model.manifest["source_sha256"]


def test_tag_collision_and_boundary_rewards_match_upstream():
    task = MockTask("pursuit", role_counts={"pursuer": 1, "evader": 1})
    model = MockMPEReward(task)
    task.positions = {"UAV_1": [0, 0, -5], "UAV_2": [0, 0, -5]}
    assert model.rewards(task) == {"UAV_1": 10, "UAV_2": -10}
    task.positions["UAV_2"] = [70, 0, -5]
    assert model.rewards(task) == {"UAV_1": 0, "UAV_2": -1}


def test_crypto_role_dependent_error_not_binary_success():
    task = MockTask("private_communication")
    model = MockMPEReward(task)
    task.guesses = {task.role_ids("receiver")[0]: task.symbol,
                    task.role_ids("eavesdropper")[0]: (task.symbol + 1) % 4}
    rewards = model.rewards(task)
    assert rewards[task.role_ids("sender")[0]] == 2
    assert rewards[task.role_ids("receiver")[0]] == 2
    assert rewards[task.role_ids("eavesdropper")[0]] == -2


@pytest.mark.parametrize("task_id", ["circle", "line"])
def test_formation_reward_optimum_matches_existing_task_slots(task_id):
    task = MockTask(task_id)
    model = MockMPEReward(task)
    for obj in task.objects:
        if obj["kind"] == "slot":
            task.positions[obj["owner"]] = obj["position"][:]
    assert all(abs(r) < 1e-12 for r in model.rewards(task).values())


def test_record_actual_decisions_and_persistent_intervals(tmp_path):
    task = MockTask("pursuit")
    task.mission_id, task.policy = "recording-test", "local_skill_baseline"
    model = MockMPEReward(task)
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    recorder = MockTrajectoryRecorder(memory, task, model)
    obs = {rid: task.observe(rid) for rid in task.roles}
    # No fabricated trajectory while waiting for the first model decision.
    recorder.open_interval(obs)
    assert recorder.close_interval(obs) == {}
    for rid in task.roles:
        recorder.selected(rid, local_skill_choice(obs[rid]), obs[rid],
                          SimpleNamespace(success=True, output={"persistent": True}), None, "local_skill_baseline")
    for _ in range(2):
        recorder.open_interval(obs)
        recorder.close_interval(obs)
    task.status = "horizon"
    recorder.finish(tmp_path)
    records = memory.records()
    assert len(records) == 8
    assert all(r.metadata["online_update"] is False and not r.metadata["reuse_allowed"] for r in records)
    assert sum(r.metadata["continued_skill"] for r in records) == 4
    assert all(r.return_finalized and r.next_state and r.invocation for r in records)
    assert (tmp_path / "experience.jsonl").is_file()
    memory.close()


@pytest.mark.parametrize("task_id", TASKS)
def test_existing_api_runner_persists_real_mock_execution(tmp_path, monkeypatch, task_id):
    import json
    import threading
    from flask import Flask
    from adapters.mock_adapter import MockAdapter
    from brain.mission_progress import MissionProgressTracker
    from runtime.exector import ExecutionResult
    import sim.mock_task_api as api

    adapter = MockAdapter(realtime_factor=20)
    adapter.connect()
    monkeypatch.setattr(api, "get_adapter", lambda: adapter)
    monkeypatch.setattr(api, "__file__", str(tmp_path / "backend/sim/mock_task_api.py"))
    memory = SwarmExperienceMemory(tmp_path / "memory.sqlite3")
    noop = lambda *args, **kwargs: None
    state = SimpleNamespace(swarm_experience_memory=memory, initialized=True, is_executing=False,
        mode="ai", _ai_stop_event=threading.Event(), executing_robot_snapshot=lambda: [],
        try_begin_robot_executions=lambda roles: (True, []), end_robot_executions=noop,
        mission_progress=MissionProgressTracker(), push_log=noop,
        get_world_snapshot=adapter.get_robot_snapshot,
        agent_contexts=SimpleNamespace(update_task=noop, set_local_links=lambda *args: []))

    def dispatch(step):
        try:
            output = adapter.mock_task.apply(step["robot"], step["skill"], step["parameters"], adapter)
            return ExecutionResult(success=True, output=output)
        except ValueError as exc:
            return ExecutionResult(success=False, error_msg=str(exc))
    state.runtime = SimpleNamespace(dispatch_skill=dispatch)
    background = []
    socket = SimpleNamespace(emit=noop, start_background_task=lambda *args: background.append(args))
    app = Flask(__name__)
    api.register_mock_tasks(app, state, socket,
        resize_fleet=lambda count, poses: adapter.retain_fleet(p["robot_id"] for p in poses),
        set_mode=noop, emit_progress=noop, send_message=noop, system_status=lambda: {}, skill_catalog=lambda: [])
    try:
        result, status = state.mock_task_start({"scenario_id": task_id, "max_rounds": 3,
            "experiment_id": "memory-integration-test", "split": "verification"})
        assert status == 200, result
        task = adapter.mock_task
        function, *args = background[0]
        function(*args)
        records = memory.records(task.mission_id)
        assert task.status in {"complete", "horizon"}
        assert len(records) == 3 * len(task.roles)
        assert all(r.immediate_reward is not None and r.return_finalized for r in records)
        assert all(r.role == task.roles[r.agent_id] for r in records)
        directory = tmp_path / "results/mock-tasks" / task.mission_id
        assert json.loads((directory / "summary.json").read_text())["experience_records"] == len(records)
        for rid in task.roles:
            rows = [r for r in records if r.agent_id == rid]
            assert rows[0].return_value == pytest.approx(sum(.95**i * r.immediate_reward for i, r in enumerate(rows)))
        assert not any(r.metadata["reuse_allowed"] for r in records)
    finally:
        adapter.disconnect()
        memory.close()
