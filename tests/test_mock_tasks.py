import json
from collections import Counter
from unittest.mock import Mock

import pytest

from adapters.mock_adapter import MockAdapter
from sim.mock_task_api import infer_task_layout, _normalize_llm_task_layout
from sim.mock_tasks import MockTask, TASKS, SKILL_NAMES, catalog, local_skill_choice


@pytest.fixture
def adapter():
    # The same point-mass integrator is stepped deterministically, with no timer thread.
    instance = MockAdapter()
    instance._connected = True
    yield instance
    instance.disconnect()


def setup_task(adapter, name, seed=0):
    task = MockTask(name, seed, max_rounds=180)
    for rid, pos in task.positions.items():
        adapter.reset_robot_pose(rid, pos, in_air=True)
    adapter.retain_fleet(task.roles)
    adapter.set_operating_bounds(task.bounds, task.roles)
    adapter.mock_task = task
    task.status = "running"
    return task


def advance(adapter, task, rounds=1):
    for _ in range(rounds):
        task.sync(adapter.get_robot_snapshot())
        for rid in task.roles:
            obs = task.observe(rid)
            choice = local_skill_choice(obs)
            assert choice in obs["options"]
            task.apply(rid, choice["skill"], choice["parameters"], adapter)
        for _ in range(10):
            task.refresh_motion(adapter)
            with adapter._state_lock:
                adapter._step_world_locked()
        task.sync(adapter.get_robot_snapshot())
        task.evaluate()


@pytest.mark.parametrize("name", TASKS)
def test_nine_forms_use_existing_mock_dynamics(adapter, name):
    task = setup_task(adapter, name)
    advance(adapter, task, 5)
    assert task.metrics
    assert not task.snapshot()["native_mpe"]
    assert all(s in SKILL_NAMES for s in task.active_skills)
    assert all(-70 <= p[0] <= 70 and -70 <= p[1] <= 70 for p in task.positions.values())


@pytest.mark.parametrize("name", ("coverage", "navigation", "circle", "line", "collection"))
def test_cooperative_objectives_are_reachable(adapter, name):
    task = setup_task(adapter, name)
    for _ in range(180):
        advance(adapter, task)
        if task.status == "complete":
            break
    assert task.status == "complete", task.metrics


def test_role_private_fields_never_reach_other_roles(adapter):
    task = setup_task(adapter, "private_communication", 7)
    assert "private_symbol" in task.observe("UAV_1")
    assert "private_symbol" not in task.observe("UAV_2")
    assert "private_key" not in task.observe("UAV_3")
    advance(adapter, task, 3)
    assert task.metrics["receiver_correct"]
    inbox = task.observe("UAV_3")["messages"]
    assert inbox and all(m["kind"] == "ciphertext" for m in inbox)
    assert "private_key" not in json.dumps(inbox)
    nav = setup_task(adapter, "navigation")
    assert "private_goal" not in nav.observe("UAV_2")
    assert not any(o["skill"] == "follow_signal" for o in nav.observe("UAV_2")["options"])
    hidden = setup_task(adapter, "concealment")
    assert "private_goal" not in hidden.observe("UAV_3")


def test_stationary_roles_cannot_invoke_motion(adapter):
    task = setup_task(adapter, "navigation")
    initial = task.positions["UAV_1"][:]
    with pytest.raises(ValueError):
        task.apply("UAV_1", "cover_landmark", {"target_id": "landmark_0"}, adapter)
    advance(adapter, task, 10)
    assert task.positions["UAV_1"] == initial


def test_bound_skill_rejects_foreign_body(adapter, monkeypatch):
    from skills.mock_task_skills import task_skill_factories
    import adapters.adapter_manager as manager
    monkeypatch.setattr(manager, "get_adapter", lambda: adapter)
    task = setup_task(adapter, "coverage")
    skill = next(factory() for factory in task_skill_factories() if factory.name == "cover_landmark")
    before = adapter.get_robot_snapshot()["UAV_2"]["command_velocity"]
    with adapter.bind_robot("UAV_1"):
        bad = skill.execute({"robot_id": "UAV_2", "target_id": "landmark_0"})
        good = skill.execute({"robot_id": "UAV_1", "target_id": "landmark_0"})
    assert not bad.success and good.success
    assert adapter.get_robot_snapshot()["UAV_2"]["command_velocity"] == before


def test_formation_has_distinct_slots_and_collection_matching_types(adapter):
    task = setup_task(adapter, "circle")
    slots = [o for o in task.objects if o["kind"] == "slot"]
    assert len({o["owner"] for o in slots}) == 5
    assert all(abs(o["position"][0]) + abs(o["position"][1] - 12) > 20 for o in slots)
    task = setup_task(adapter, "collection")
    treasure = task.objects[0]
    adapter.reset_robot_pose("UAV_1", treasure["position"], in_air=True)
    task.sync(adapter.get_robot_snapshot())
    task.apply("UAV_1", "collect_treasure", {"target_id": treasure["id"]}, adapter)
    assert task.cargo["UAV_1"]["color"] == "red"
    assert not any(o["skill"] == "collect_treasure" for o in task.observe("UAV_1")["options"])
    with pytest.raises(ValueError):
        task.apply("UAV_1", "deliver_treasure", {"target_id": "UAV_4"}, adapter)


def test_leader_is_mobile_and_cover_restricts_ordinary_pursuer_sensing(adapter):
    task = setup_task(adapter, "world_communication")
    forest = next(o for o in task.objects if o["kind"] == "forest")
    task.positions["UAV_4"] = forest["position"][:]
    task.positions["UAV_2"] = [forest["position"][0] - 15, forest["position"][1], -5]
    assert "explore_local" in task.skills("UAV_1")
    assert "UAV_4" not in {n["id"] for n in task.observe("UAV_2")["neighbors"]}
    assert "UAV_4" in {n["id"] for n in task.observe("UAV_1")["neighbors"]}


def test_task_diagnostics_do_not_pollute_reward_memory():
    from brain.mission_progress import MissionProgressTracker
    tracker = MissionProgressTracker()
    memory = Mock()
    tracker.set_experience_memory(memory)
    tracker.start("test", "Mock", "", [{"robot_id": "UAV_1", "task": "test"}], [], 0, record_experience=False)
    tracker.record_result("test", "UAV_1", True)
    tracker.record_termination_vote("test", "UAV_1", True, "done")
    assert not memory.mock_calls


def test_repeatable_initialization_and_catalog():
    assert len(catalog()) == 9
    for name in TASKS:
        assert MockTask(name, 9).snapshot() == MockTask(name, 9).snapshot()


def test_role_quantities_override_defaults_and_reset_layout():
    task = MockTask("pursuit", 9, role_counts={"pursuer": 5, "evader": 1})
    assert len(task.roles) == 6
    assert Counter(task.roles.values()) == Counter({"pursuer": 5, "evader": 1})
    assert set(task.positions) == set(task.roles)
    assert task.snapshot()["role_counts"] == {"pursuer": 5, "evader": 1}

    total = MockTask("coverage", 9, fleet_size=6)
    assert len(total.roles) == 6
    assert set(total.roles.values()) == {"searcher"}

    explicit = MockTask("pursuit", 9, role_assignments=[
        {"robot_id": "UAV1", "role": "pursuer"},
        {"robot_id": "UAV2", "role": "pursuer"},
        {"robot_id": "UAV_3", "role": "evader"},
    ])
    assert explicit.roles == {"UAV_1": "pursuer", "UAV_2": "pursuer", "UAV_3": "evader"}
    assert explicit.snapshot()["requested_role_assignments"] == explicit.roles


def test_multi_evader_pursuit_keeps_all_requested_bodies_and_local_targets(adapter):
    task = MockTask("pursuit", 4, max_rounds=60, role_counts={"pursuer": 4, "evader": 2})
    for rid, pos in task.positions.items():
        adapter.reset_robot_pose(rid, pos, in_air=True)
    adapter.retain_fleet(task.roles)
    adapter.set_operating_bounds(task.bounds, task.roles)

    targets = {
        rid: task.observe(rid).get("pursuit_target_id")
        for rid, role in task.roles.items()
        if role == "pursuer"
    }
    assert task.snapshot()["role_counts"] == {"pursuer": 4, "evader": 2}
    assert targets == {
        "UAV_1": "UAV_5",
        "UAV_2": "UAV_6",
        "UAV_3": "UAV_5",
        "UAV_4": "UAV_6",
    }

    advance(adapter, task, 5)
    assert task.metrics["evader_count"] == 2
    assert task.metrics["near_collisions"] == 0


def test_local_agent_system_prompts_bind_role_and_task_context(adapter):
    task = MockTask("pursuit", 4, role_counts={"pursuer": 4, "evader": 2})
    pursuer_prompt = task.observe("UAV_1")["agent_system_prompt"]
    evader_prompt = task.observe("UAV_5")["agent_system_prompt"]
    assert "UAV_1" in pursuer_prompt
    assert "pursuer" in pursuer_prompt
    assert "UAV_5" in pursuer_prompt
    assert "evader" in evader_prompt
    assert pursuer_prompt != evader_prompt


def test_llm_task_layout_is_normalized_before_mock_reset():
    layout = _normalize_llm_task_layout({
        "scenario_id": "pursuit",
        "role_assignments": [
            {"robot_id": "UAV1", "role": "pursuer"},
            {"robot_id": "UAV2", "role": "pursuer"},
            {"robot_id": "UAV3", "role": "evader"},
        ],
    })
    assert layout == {
        "scenario_id": "pursuit",
        "role_assignments": [
            {"robot_id": "UAV_1", "role": "pursuer"},
            {"robot_id": "UAV_2", "role": "pursuer"},
            {"robot_id": "UAV_3", "role": "evader"},
        ],
    }
    assert _normalize_llm_task_layout({"task_id": None}) is None
    with pytest.raises(ValueError):
        _normalize_llm_task_layout({"task_id": "pursuit", "role_counts": {"searcher": 2}})


def test_natural_language_quantities_are_parsed_for_task_start():
    layout = infer_task_layout("请执行追逐任务，5个追捕者和1个逃跑者，seed 4")
    assert layout == {"role_counts": {"pursuer": 5, "evader": 1}, "fleet_size": None}
    range_layout = infer_task_layout("UAV1-4追逐UAV5-6")
    assert range_layout == {"role_counts": {"pursuer": 4, "evader": 2}, "fleet_size": None}
    compact_layout = infer_task_layout("UAV1-6 追 7")
    assert compact_layout == {"role_counts": {"pursuer": 6, "evader": 1}, "fleet_size": None}
    compact_range_layout = infer_task_layout("UAV1-6 追 7-8")
    assert compact_range_layout == {"role_counts": {"pursuer": 6, "evader": 2}, "fleet_size": None}
    assert infer_task_layout("pursuit with 7 UAVs")["fleet_size"] == 7
    assert infer_task_layout("协同搜索，6架无人机")["fleet_size"] == 6
