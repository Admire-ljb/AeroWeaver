import importlib.util
import json
import os
from pathlib import Path
import time

import pytest

spec = importlib.util.spec_from_file_location("activation_closed_loop", Path(__file__).with_name("run.py"))
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)
CATALOG = Path(os.environ.get("ACTIVATION_CATALOG", run.ROOT / "experiments/skill_activation_v1/catalog.json"))


class StubClient:
    def __init__(self, invalid=False):
        self.invalid = invalid
        self.calls = []

    def call(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        result = {"skills": ["cover_landmark", "pursue_target", "follow_peer", "evade_peer", "explore_local", "hold_position"]} if kwargs["context"]["phase"] == "activation" else {"option_index": -1 if self.invalid else 0}
        return result, {"usage": {"total_tokens": 10}, "latency_s": .001}


def test_activation_is_whole_mission_not_role_query():
    client = StubClient()
    skills = json.loads(CATALOG.read_text())
    active, meta = run.activate("semantic_topk", run.TASK_TEXT["pursuit"], skills, 6, client, {})
    sent = json.loads(client.calls[0][0][1]["content"])
    assert "role" not in sent and "evader" in sent["mission"]
    assert meta["scope"] == "whole_mission"
    assert "hold_position" in active


def test_catalog_reaches_bodies_without_static_role_template_replacement():
    skills = json.loads(CATALOG.read_text())
    active, _ = run.activate("full_catalog", run.TASK_TEXT["coverage"], skills, 6)
    task = run.ActivatedTask("coverage", active)
    assert len(task.observe("UAV_1")["skills"]) == 22
    narrow = run.ActivatedTask("coverage", ["hold_position"])
    assert narrow.observe("UAV_1")["options"] == [{"skill": "hold_position", "parameters": {}}]


def test_identical_initial_state_and_fixed_opponent():
    a = run.ActivatedTask("pursuit", ["hold_position"], seed=101)
    b = run.ActivatedTask("pursuit", ["pursue_target", "explore_local", "hold_position"], seed=101)
    assert a.positions == b.positions and a.objects == b.objects
    assert a.observe("UAV_4")["skills"] == b.observe("UAV_4")["skills"]
    assert run.local_skill_choice(a.observe("UAV_4")) == run.local_skill_choice(b.observe("UAV_4"))


def test_private_skill_preconditions():
    active = [s["name"] for s in json.loads(CATALOG.read_text())]
    task = run.ActivatedTask("coverage", active)
    options = task.observe("UAV_1")["options"]
    assert any(o["skill"] == "cover_landmark" for o in options)
    assert not any(o["skill"] == "infer_goal" for o in options)


def test_simulation_only_advances_by_explicit_ticks():
    task = run.ActivatedTask("coverage", ["cover_landmark", "hold_position"])
    adapter = run.FixedStepAdapter()
    adapter.connect()
    adapter.retain_fleet(task.roles)
    for rid in task.roles:
        adapter.reset_robot_pose(rid, task.positions[rid])
    task.sync(adapter.get_robot_snapshot())
    task.apply("UAV_1", "cover_landmark", {"target_id": "landmark_0"}, adapter)
    before = adapter.get_robot_snapshot()
    time.sleep(.08)
    assert before == adapter.get_robot_snapshot()
    adapter.advance(task, 10)
    assert before != adapter.get_robot_snapshot()
    assert adapter._physics_thread is None
    adapter.disconnect()


@pytest.mark.parametrize("scenario", run.SCENARIOS)
def test_recording_and_discount_verification(tmp_path, scenario):
    row = run.run_episode((scenario, "full_catalog", 99), output=tmp_path,
        skills=json.loads(CATALOG.read_text()), client=StubClient(), memory_path=tmp_path / "memory.sqlite3",
        experiment_id="test", rounds=3)
    assert row["status"] == "horizon", row
    assert row["decision_errors"] == row["invocation_errors"] == 0
    check = run.verify(tmp_path, [row], (tmp_path / "memory.sqlite3").resolve())
    assert check["records_verified"] == 3 * len(row["roles"])
    records = [json.loads(line) for line in (tmp_path / "episodes" / row["episode_id"] / "experience.jsonl").read_text().splitlines()]
    assert all(row["metadata"]["truncated"] for row in records[-len(row["roles"]):])


def test_invalid_decision_records_actual_hold_not_unexecuted_skill(tmp_path):
    row = run.run_episode(("coverage", "full_catalog", 100), output=tmp_path,
        skills=json.loads(CATALOG.read_text()), client=StubClient(invalid=True),
        memory_path=tmp_path / "memory.sqlite3", experiment_id="test", rounds=1)
    assert row["decision_errors"] == 3
    records = [json.loads(line) for line in (tmp_path / "episodes" / row["episode_id"] / "experience.jsonl").read_text().splitlines()]
    assert all(r["skill"] == "hold_position" and not r["success"] for r in records)
    assert all(r["trace"]["decision"]["error"] for r in records)
