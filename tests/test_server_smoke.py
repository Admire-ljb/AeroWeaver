import importlib
import threading
from types import SimpleNamespace


def test_server_imports_and_exposes_flask_app():
    server = importlib.import_module("server")
    assert server.app is not None
    assert server.socketio is not None


def test_status_endpoint_responds_with_system_state():
    server = importlib.import_module("server")
    client = server.app.test_client()
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert "initialized" in payload
    assert "mode" in payload

def _portable(path):
    return path.replace("\\", "/")


def test_runtime_layout_supports_repository_and_flat_deployments():
    server = importlib.import_module("server")

    _, repository_root, repository_ui = server._resolve_runtime_layout(
        "C:/workspace/AeroWeaver/backend/server.py"
    )
    assert _portable(repository_root).endswith("/AeroWeaver")
    assert _portable(repository_ui).endswith("/AeroWeaver/frontend/dist")

    _, flat_root, flat_ui = server._resolve_runtime_layout(
        "/home/work3/AeroWeaver/server.py"
    )
    assert _portable(flat_root).endswith("/home/work3/AeroWeaver")
    assert _portable(flat_ui).endswith("/home/work3/AeroWeaver/ui/dist")


def test_root_route_never_falls_back_to_legacy_dashboard(tmp_path, monkeypatch):
    server = importlib.import_module("server")
    empty_dist = tmp_path / "dist"
    empty_dist.mkdir()
    monkeypatch.setattr(server, "_UI_DIST", str(empty_dist))

    response = server.app.test_client().get("/")

    assert response.status_code == 503
    assert b"AeroWeaver frontend is not built" in response.data
    assert b"BodySense" not in response.data


def test_skill_inventory_query_uses_live_registry(monkeypatch):
    server = importlib.import_module("server")
    monkeypatch.setattr(server.state, "current_robot", "UAV_1")
    monkeypatch.setattr(server, "_get_skill_catalog", lambda robot_id=None: {
        "UAV_1": [{"name": "takeoff"}, {"name": "land"}, {"name": "hover"}],
        "UAV_2": [{"name": "takeoff"}, {"name": "land"}, {"name": "hover"}],
    })

    reply = server._build_skill_inventory_reply("现在有多少注册的技能")

    assert "UAV_1 注册了 3 个" in reply
    assert "2 架已注册无人机" in reply
    assert "去重为 3 个" in reply
    assert "实例总数为 6 个" in reply


def test_non_inventory_message_is_not_intercepted():
    server = importlib.import_module("server")

    assert server._build_skill_inventory_reply("让 UAV_1 起飞") is None


def test_mock_registry_hides_sensor_dependent_skills(monkeypatch):
    server = importlib.import_module("server")
    monkeypatch.setenv("SIM_ADAPTER", "mock")

    registry, count = server._build_robot_registry("UAV_1", "UAV")
    catalog_names = {entry["name"] for entry in registry.get_skill_catalog()}

    assert registry.adapter_profile == "mock"
    assert count == len(registry)
    assert {
        "takeoff", "land", "fly_to", "fly_relative", "hover",
        "get_position", "get_battery", "look_around", "report",
    } <= catalog_names
    assert server._MOCK_HIDDEN_SENSOR_SKILLS.isdisjoint(catalog_names)
    assert "area_recon" not in catalog_names
    assert registry.allows_soft_skill("area_recon") is False


def test_mock_profile_hides_document_skill_api(monkeypatch):
    server = importlib.import_module("server")
    monkeypatch.setenv("SIM_ADAPTER", "mock")
    registry, _ = server._build_robot_registry("UAV_1", "UAV")
    monkeypatch.setattr(server.state, "robot_registries", {"UAV_1": registry})
    client = server.app.test_client()

    listing = client.get("/api/skills/soft")
    creation = client.post(
        "/api/skills/soft",
        json={"name": "mock_hidden_strategy", "content": "# hidden"},
    )

    assert listing.status_code == 200
    assert listing.get_json()["count"] == 0
    assert listing.get_json()["skills"] == []
    assert creation.status_code == 409
    assert creation.get_json()["adapter_profile"] == "mock"


def test_global_motion_stop_brakes_every_assigned_uav(monkeypatch):
    server = importlib.import_module("server")
    calls = []

    class FakeAdapter:
        def request_stop(self):
            calls.append(("interrupt", None))

        def stop_velocity_for(self, robot_id):
            calls.append(("brake", robot_id))
            return SimpleNamespace(success=True)

    adapter = FakeAdapter()
    monkeypatch.setattr("adapters.adapter_manager.get_all_adapters", lambda: [adapter])
    monkeypatch.setattr("adapters.adapter_manager.get_primary_adapter", lambda: adapter)

    stopped = server._stop_all_adapter_motion(["UAV_1", "UAV_2", "UAV_3"])

    assert calls.count(("interrupt", None)) == 1
    assert [(kind, robot) for kind, robot in calls if kind == "brake"] == [
        ("brake", "UAV_1"),
        ("brake", "UAV_2"),
        ("brake", "UAV_3"),
    ]
    assert stopped == ["UAV_1", "UAV_2", "UAV_3"]


def test_global_stop_cancels_mission_agents_and_communication(monkeypatch):
    server = importlib.import_module("server")
    from brain.mission_progress import MissionProgressTracker
    from brain.uav_agent_context import UAVAgentContextStore

    tracker = MissionProgressTracker()
    tracker.start(
        "mission-stop",
        "Search the task area",
        "Split the area",
        [
            {"robot_id": "UAV_1", "task": "west"},
            {"robot_id": "UAV_2", "task": "east"},
        ],
        [],
        10,
    )
    contexts = UAVAgentContextStore()
    contexts.establish_links(["UAV_1", "UAV_2"], "mission-stop")
    stop_event = threading.Event()

    monkeypatch.setattr(server.state, "mission_progress", tracker)
    monkeypatch.setattr(server.state, "agent_contexts", contexts)
    monkeypatch.setattr(server.state, "_ai_stop_event", stop_event)
    monkeypatch.setattr(server.state, "is_executing", False)
    monkeypatch.setattr(server.state, "_current_agent_loop", None)
    monkeypatch.setattr(server.state, "executing_robot_snapshot", lambda: ["UAV_1", "UAV_2"])
    monkeypatch.setattr(server.state, "get_world_snapshot", lambda: {"robots": {}})
    monkeypatch.setattr(server.state, "push_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_stop_all_adapter_motion", lambda robot_ids: list(robot_ids))
    monkeypatch.setattr(server, "_emit_commander_progress", lambda sid: tracker.snapshot())
    monkeypatch.setattr(server.socketio, "emit", lambda *args, **kwargs: None)

    result = server._stop_current_task()
    mission = tracker.snapshot()
    context_snapshot = contexts.snapshot()

    assert result["stopped"] is True
    assert result["status"] == "cancelled"
    assert result["robot_ids"] == ["UAV_1", "UAV_2"]
    assert stop_event.is_set()
    assert mission["status"] == "cancelled"
    assert all(agent["status"] == "cancelled" for agent in mission["agents"])
    assert context_snapshot["contexts"]["UAV_1"]["status"] == "cancelled"
    assert not any(link["status"] == "active" for link in context_snapshot["links"])


def test_real_adapter_registry_keeps_perception_and_document_skills(monkeypatch):
    server = importlib.import_module("server")
    monkeypatch.setenv("SIM_ADAPTER", "airsim")

    registry, _ = server._build_robot_registry("UAV_1", "UAV")
    catalog_names = {entry["name"] for entry in registry.get_skill_catalog()}

    assert registry.adapter_profile == "default"
    assert {"observe", "perceive", "detect_object", "get_sensor_data"} <= catalog_names
    assert "area_recon" in catalog_names
    assert registry.allows_soft_skill("area_recon") is True
