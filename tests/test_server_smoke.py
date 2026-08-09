import importlib


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
