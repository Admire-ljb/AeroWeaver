"""No-network tests for all-task interfaces and single-component switches."""

from functools import partial
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from pathlib import Path

import pytest

import pilot
import run_all_tasks as suite
from test_pilot import StubClient


@pytest.fixture(autouse=True)
def task_configuration():
    suite.configure_support()


def catalog():
    return json.loads((Path(__file__).parent / "catalog.json").read_text())


def test_schedule_is_54_unique_paired_episodes():
    specs = suite.build_specs(66001)
    assert len(specs) == len(set(specs)) == 54
    assert {seed for _, _, seed in specs} == {66001}
    assert {task for task, _, _ in specs} == set(suite.SCENARIOS)
    assert {method for _, method, _ in specs} == set(suite.CONDITIONS)


def test_global_request_limit_covers_nested_agent_threads():
    client = object.__new__(pilot.Client)
    client.request_slots = threading.BoundedSemaphore(3)
    lock = threading.Lock()
    counts = {"active": 0, "peak": 0, "calls": 0}

    def request(*args):
        with lock:
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
        time.sleep(.02)
        with lock:
            counts["active"] -= 1
            counts["calls"] += 1

    client._request = request
    with ThreadPoolExecutor(max_workers=15) as pool:
        list(pool.map(lambda _: client.request([], {}), range(30)))
    assert counts == {"active": 0, "peak": 3, "calls": 30}


@pytest.mark.parametrize("scenario", suite.SCENARIOS)
@pytest.mark.parametrize("method", suite.CONDITIONS)
def test_all_task_episode(tmp_path, scenario, method):
    skills = catalog()
    active = [s["name"] for s in skills]
    path = tmp_path / "memory.sqlite3"
    row = pilot.run_episode(scenario, method, 66101, "synthetic_preflight", tmp_path,
                            skills, StubClient(), path, rounds=4, active_override=active,
                            task_factory=partial(suite.ExperimentTask, condition=method))
    assert row["status"] in {"complete", "horizon"}, row.get("traceback")
    assert row["decision_errors"] == row["invocation_errors"] == 0
    events = [json.loads(line) for line in (tmp_path / "episodes" / row["episode_id"] / "trace.jsonl").read_text().splitlines()]
    memory = pilot.support.SwarmExperienceMemory(path)
    try:
        records = memory.records(row["episode_id"])
        assert len(records) == row["records"] == row["rounds"] * len(row["roles"])
        assert all(record.return_finalized for record in records)
        for record in records:
            controlled = record.role not in suite.FIXED_ROLES[scenario]
            assert record.metadata["reuse_allowed"] == (controlled and method in pilot.LOCAL_METHODS)
        for event in events:
            for rid, decision in event["decisions"].items():
                if rid in row["roles"] and row["roles"][rid] in suite.FIXED_ROLES[scenario]:
                    assert decision["source"] == "fixed_opponent"
                if decision.get("ranking"):
                    ranking = decision["ranking"]
                    assert ranking["beta"] == (0 if method == "aeroweaver_no_rl" else .8)
                    if method == "aeroweaver_no_rl":
                        assert ranking["selected"] == ranking["base_selected"]
                        assert not ranking["changed_by_memory"]
            if method == "aeroweaver_no_peer":
                assert all(message["kind"] not in {"intent", "target"}
                           or row["roles"][message["source"]] in suite.FIXED_ROLES[scenario]
                           for message in event["messages"])
    finally:
        memory.close()


@pytest.mark.parametrize("scenario", ["navigation", "private_communication"])
def test_stationary_catalog_and_private_observations(scenario):
    task = suite.ExperimentTask(scenario, [s["name"] for s in catalog()], condition="aeroweaver_full_catalog", seed=66101)
    for rid, role in task.roles.items():
        obs = pilot.compact(task.observe(rid))
        if role in pilot.support.STATIONARY:
            assert all(option["skill"] in {"signal_goal", "encode_message", "decode_message", "guess_message", "hold_position"}
                       for option in obs["options"])
        if role in {"listener", "eavesdropper"}:
            assert "private_goal" not in obs and "private_key" not in obs and "private_symbol" not in obs
        if role == "receiver":
            assert "private_key" in obs and "private_symbol" not in obs


@pytest.mark.parametrize("scenario,kind,content", [
    ("navigation", "goal", {"target_id": "landmark_0"}),
    ("private_communication", "ciphertext", {"symbol": 2}),
])
def test_no_peer_preserves_intrinsic_channels(scenario, kind, content):
    task = suite.ExperimentTask(scenario, [s["name"] for s in catalog()], condition="aeroweaver_no_peer", seed=66101)
    task._send("UAV_1", kind, content)
    assert task.messages and all(message["kind"] == kind for message in task.messages)


def test_no_peer_suppresses_coordination_reports():
    task = suite.ExperimentTask("pursuit", [s["name"] for s in catalog()], condition="aeroweaver_no_peer", seed=66101)
    task._send("UAV_1", "target", {"target_id": "UAV_4", "position": [0, 0, -5]})
    task._send("UAV_1", "intent", {"skill": "pursue_target"})
    assert task.suppressed_coordination_messages > 0 and task.messages == []


def test_shared_activation_is_not_silently_repaired(tmp_path):
    class BadActivation(StubClient):
        def request(self, *args, **kwargs):
            return {"choices": [{"message": {"content": '{"skills":["invented"]}'}}]}
    with pytest.raises(ValueError, match="Invalid shared"):
        suite.activate_once(BadActivation(), "coverage", catalog(), tmp_path)


def test_private_pairing_state_is_recorded(tmp_path):
    skills = catalog()
    row = pilot.run_episode("private_communication", "aeroweaver_no_rl", 66101, "fixture", tmp_path,
                            skills, StubClient(), tmp_path / "memory.sqlite3", rounds=3,
                            active_override=[s["name"] for s in skills],
                            task_factory=partial(suite.ExperimentTask, condition="aeroweaver_no_rl"))
    state = json.loads((tmp_path / "episodes" / row["episode_id"] / "initial_state.json").read_text())
    assert set(state["private_state_for_pairing_only"]) == {"goal", "symbol", "key"}
    assert row["task_success"] == (row["final_metrics"]["receiver_correct"] and not row["final_metrics"]["eavesdropper_correct"])


@pytest.mark.parametrize("restart", [False, True])
def test_continuation_preserves_evidence_and_starts_with_empty_memory(tmp_path, restart):
    parent, output = tmp_path / "parent", tmp_path / "new"
    specs = suite.build_specs(66001)[:3]
    output.mkdir()
    pilot.support.dump(parent / "plan.json", {"model": "deepseek-v4-flash"})
    (parent / "memory").mkdir()
    (parent / "shared_activations").mkdir()
    (parent / "calls.jsonl").write_text('{"retained":"raw"}\n')
    for i, (scenario, method, seed) in enumerate(specs):
        row = {"episode_id": f"fixture-{i}", "scenario": scenario, "method": method, "seed": seed,
               "status": "horizon" if i == 0 else "technical_failure", "calls": [4, 0, 2][i], "records": [8, 0, 4][i]}
        pilot.support.dump(parent / "episodes" / row["episode_id"] / "summary.json", row)
        (parent / "memory" / f"{scenario}-{method}.sqlite3").write_bytes(b"fixture")
    before = {str(p.relative_to(parent)): p.read_bytes() for p in parent.rglob("*") if p.is_file()}
    scheduled, retained, replacements = suite.prepare_continuation(parent, output, specs, restart)
    assert scheduled == (specs[1:] if restart else specs[1:2])
    assert len(retained) == (1 if restart else 2)
    assert all(row["model"] == "deepseek-v4-flash" for row in retained)
    assert replacements == ({specs[2]: {"episode_id": "fixture-2-restart", "restarted_from": "fixture-2"}} if restart else {})
    for scenario, method, _ in scheduled:
        assert not (output / "memory" / f"{scenario}-{method}.sqlite3").exists()
    if restart:
        assert (output / "archived_attempts/fixture-2/memory.sqlite3").read_bytes() == b"fixture"
    assert (output / "calls.jsonl").read_bytes() == (parent / "calls.jsonl").read_bytes()
    assert before == {str(p.relative_to(parent)): p.read_bytes() for p in parent.rglob("*") if p.is_file()}


def test_restarted_episode_uses_distinct_memory_identity(tmp_path):
    skills = catalog()
    row = pilot.run_episode("pursuit", "aeroweaver_no_rl", 66101, "single_episode", tmp_path,
                            skills, StubClient(), tmp_path / "memory.sqlite3", rounds=1,
                            active_override=[s["name"] for s in skills], episode_id="explicit-restart",
                            task_factory=partial(suite.ExperimentTask, condition="aeroweaver_no_rl"))
    assert row["episode_id"] == "explicit-restart" and row["status"] == "horizon"
    records = [json.loads(line) for line in (tmp_path / "episodes/explicit-restart/experience.jsonl").read_text().splitlines()]
    assert records and all(record["mission_id"] == "explicit-restart" for record in records)
