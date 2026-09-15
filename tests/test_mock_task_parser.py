import ast
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
import pytest

from adapters.mock_adapter import MockAdapter
import adapters.adapter_manager as adapter_manager
import llm_client
import sim.mock_task_api as api
from sim.mock_tasks import MockTask, TASKS


def pursuit_layout(count=8):
    return {
        "scenario_id": "pursuit",
        "role_assignments": [
            {"robot_id": f"UAV_{i}", "role": "pursuer" if i <= count - 2 else "evader"}
            for i in range(1, count + 1)
        ],
    }


def test_parser_supplies_scenario_roles_and_room_for_ten_bindings(monkeypatch):
    client = Mock()
    client.chat.return_value = json.dumps(pursuit_layout(10))
    monkeypatch.setattr(llm_client, "get_client", lambda **kwargs: client)
    layout = api.llm_infer_task_layout("UAV1-8 chase 9-10", ["UAV_1", "UAV_2"])
    assert layout == pursuit_layout(10)
    messages = client.chat.call_args.args[0]
    request = json.loads(messages[1]["content"])
    assert len(request["scenario_catalog"]) == 9
    scenario = next(x for x in request["scenario_catalog"] if x["id"] == "pursuit")
    assert scenario["minimum_roles"] == {"pursuer": 1, "evader": 1}
    assert client.chat.call_args.kwargs["max_tokens"] >= 1000
    assert "not a limit" in messages[0]["content"]


@pytest.mark.parametrize("scenario_id", TASKS)
def test_explicit_default_roles_instantiate_every_scenario(scenario_id):
    layout = api._normalize_llm_task_layout({
        "scenario_id": scenario_id,
        "role_assignments": [
            {"robot_id": f"UAV_{i + 1}", "role": role}
            for i, role in enumerate(TASKS[scenario_id][2])
        ],
    })
    task = MockTask(layout["scenario_id"], role_assignments=layout["role_assignments"])
    for rid, role in task.roles.items():
        assert role in task.observe(rid)["agent_system_prompt"]


def test_binding_order_does_not_reassign_evaders_to_last_ids():
    layout = pursuit_layout()
    layout["role_assignments"][0]["role"] = "evader"
    layout["role_assignments"][-1]["role"] = "pursuer"
    layout["role_assignments"].reverse()
    task = MockTask("pursuit", role_assignments=api._normalize_llm_task_layout(layout)["role_assignments"])
    assert task.roles["UAV_1"] == "evader"
    assert task.roles["UAV_8"] == "pursuer"


@pytest.mark.parametrize("assignments", [
    [{"robot_id": "UAV_1", "role": "evader"}],
    [{"robot_id": "UAV_1", "role": "pursuer"}] * 2,
    [{"robot_id": "UAV_1", "role": "pursuer"}, {"robot_id": "UAV_3", "role": "evader"}],
    pursuit_layout(11)["role_assignments"],
])
def test_invalid_rosters_fail_before_instantiation(assignments):
    with pytest.raises(ValueError):
        api._normalize_llm_task_layout({"scenario_id": "pursuit", "role_assignments": assignments})


def commander_dispatch(monkeypatch, layout, adapter_name="mock", error=None):
    # Isolate the actual Mock dispatch branch without booting the server's background loops.
    source = Path(api.__file__).parents[1] / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_run_commander_input")
    function.body = [function.body[0]]
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    parser = Mock(return_value=layout, side_effect=error)
    monkeypatch.setattr(api, "llm_infer_task_layout", parser)
    monkeypatch.setattr(adapter_manager, "get_adapter", lambda: SimpleNamespace(name=adapter_name))
    state = Mock()
    state.mock_task_start.return_value = ({"task": {
        "title": "Pursuit", "role_counts": {"pursuer": 6, "evader": 2},
        "roles": dict.fromkeys([f"UAV_{i}" for i in range(1, 9)], "pursuer"),
        "seed": 0, "mission_id": "unit-test",
    }}, 200)
    context = {
        "state": state, "logger": logging.getLogger(__name__),
        "_active_uav_states": lambda: {"UAV_1": {}, "UAV_2": {}},
        "_emit_uav_agent_reply": Mock(),
    }
    exec(compile(module, str(source), "exec"), context)
    context["_run_commander_input"]("UAV1-6 chase 7-8", "test", "mission", "test-sid")
    return state, parser


def test_commander_passes_model_bindings_to_env(monkeypatch):
    state, parser = commander_dispatch(monkeypatch, pursuit_layout())
    parser.assert_called_once()
    payload = state.mock_task_start.call_args.args[0]
    assert payload["scenario_id"] == "pursuit"
    assert payload["role_assignments"] == pursuit_layout()["role_assignments"]
    assert payload["fleet_size"] == 8
    assert payload["parser_source"] == "llm"


def test_non_scenario_response_does_not_trigger_keyword_fallback(monkeypatch):
    rules = Mock(side_effect=AssertionError("null is not a parser failure"))
    monkeypatch.setattr(api, "infer_task_id", rules)
    state, _ = commander_dispatch(monkeypatch, None)
    state.mock_task_start.assert_not_called()
    rules.assert_not_called()


def test_non_mock_adapter_does_not_call_mock_parser(monkeypatch):
    state, parser = commander_dispatch(monkeypatch, pursuit_layout(), "airsim")
    parser.assert_not_called()
    state.mock_task_start.assert_not_called()


def test_unavailable_model_reports_rule_fallback(monkeypatch):
    state, _ = commander_dispatch(monkeypatch, None, error=RuntimeError("offline"))
    payload = state.mock_task_start.call_args.args[0]
    assert payload["parser_source"] == "rule_fallback"
    assert payload["fleet_size"] == 8


def test_scenario_api_resets_all_eight_bodies_without_starting_motion(monkeypatch):
    adapter = MockAdapter()
    adapter._connected = True
    monkeypatch.setattr(api, "get_adapter", lambda: adapter)
    state = Mock()
    state.initialized = True
    state.is_executing = False
    state.executing_robot_snapshot.return_value = []
    state.mission_progress.snapshot.return_value = {"status": "idle"}
    state.try_begin_robot_executions.return_value = (True, [])
    state._ai_stop_event = threading.Event()
    socket = Mock()
    app = Flask(__name__)
    resize = Mock(side_effect=lambda count, poses: adapter.retain_fleet(p["robot_id"] for p in poses))
    api.register_mock_tasks(
        app, state, socket, resize_fleet=resize, set_mode=Mock(),
        emit_progress=Mock(), send_message=Mock(), system_status=lambda: {}, skill_catalog=lambda: [],
    )
    try:
        response = app.test_client().post("/api/mock/tasks/start", json={
            **pursuit_layout(), "policy": "llm", "parser_source": "llm",
            "task_area": {"north_min": -90, "north_max": 65, "east_min": -72, "east_max": 90},
        })
        assert response.status_code == 200, response.json
        snapshot = response.json["task"]
        assert snapshot["scenario_id"] == "pursuit"
        assert snapshot["parser_source"] == "llm"
        assert snapshot["role_counts"] == {"pursuer": 6, "evader": 2}
        assert set(adapter.get_robot_snapshot()) == set(snapshot["roles"])
        assert snapshot["requested_role_assignments"] == snapshot["roles"]
        assert snapshot["bounds"]["north_min"] == -90
        assert resize.call_args.args[0] == 8
        socket.start_background_task.assert_called_once()
    finally:
        adapter.disconnect()
