import ast
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from adapters import adapter_manager as manager


@pytest.fixture
def isolated_manager(monkeypatch):
    monkeypatch.setattr(manager, "_adapter", None)
    monkeypatch.setattr(manager, "_adapter_type", "")
    monkeypatch.setattr(manager, "_adapter_connection_str", "")
    monkeypatch.setattr(manager, "_adapter_timeout", 15.0)
    monkeypatch.setattr(manager, "_robot_adapters", {})
    monkeypatch.setenv("AEROWEAVER_ALLOW_MOCK_FALLBACK", "0")
    yield
    if manager._adapter is not None:
        manager._adapter.disconnect()


@pytest.mark.parametrize("kind", ["airsim", "airsim_physics"])
@pytest.mark.parametrize("failure", ["refused", "exception", "missing_sdk"])
def test_air_sim_unavailable_starts_connected_mock(monkeypatch, isolated_manager, kind, failure):
    failed = Mock(name="failed_air_sim")
    failed.name = kind
    failed.description = "test adapter"
    failed.connect.return_value = False
    if failure == "exception":
        failed.connect.side_effect = ConnectionError("RPC unavailable")
    if failure == "missing_sdk":
        monkeypatch.delitem(manager._ADAPTER_REGISTRY, kind, raising=False)
    else:
        monkeypatch.setitem(manager._ADAPTER_REGISTRY, kind, lambda: failed)
    assert manager.init_startup_adapter(kind, "127.0.0.1:41451", 0.1)
    adapter = manager.get_primary_adapter()
    assert adapter.name == "mock"
    assert adapter.is_connected()
    assert manager._adapter_type == "mock"
    assert manager._adapter_connection_str == "mock://"
    assert manager.get_robot_adapter("UAV_2") is adapter


def test_connected_air_sim_is_preserved(monkeypatch, isolated_manager):
    adapter = Mock()
    adapter.name = "airsim_openfly"
    adapter.description = "test adapter"
    adapter.connect.return_value = True
    monkeypatch.setitem(manager._ADAPTER_REGISTRY, "airsim", lambda: adapter)
    assert manager.init_startup_adapter("airsim", "127.0.0.1:41451")
    assert manager.get_primary_adapter() is adapter
    adapter.disconnect.assert_not_called()


def test_startup_default_is_mock(isolated_manager):
    assert manager.init_startup_adapter()
    assert manager.get_primary_adapter().name == "mock"
    assert manager.get_primary_adapter().is_connected()


def test_px4_failure_stays_disconnected(monkeypatch, isolated_manager):
    adapter = Mock()
    adapter.name = "px4"
    adapter.description = "test adapter"
    adapter.connect.return_value = False
    monkeypatch.setitem(manager._ADAPTER_REGISTRY, "px4", lambda: adapter)
    assert not manager.init_startup_adapter("px4")
    assert manager.get_primary_adapter() is adapter


@pytest.mark.parametrize("configured", ["airsim", ""])
def test_server_startup_seeds_mock_and_emits_status(monkeypatch, isolated_manager, configured):
    # Load only the startup function; importing server would start unrelated I/O.
    source = Path(os.environ.get("AEROWEAVER_STARTUP_SERVER_SOURCE",
                                str(Path(__file__).resolve().parents[1] / "backend/server.py")))
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_try_connect_adapter")
    monkeypatch.setenv("SIM_ADAPTER", configured)
    monkeypatch.delenv("AEROWEAVER_FORCE_GZ_SENSOR_BRIDGE", raising=False)
    failed = Mock()
    failed.name, failed.description = "airsim_openfly", "test"
    failed.connect.return_value = False
    monkeypatch.setitem(manager._ADAPTER_REGISTRY, "airsim", lambda: failed)
    state = Mock()
    state.world_model.get_world_state.return_value = {
        "robots": {"UAV_1": {"position": [1, 2, -5], "in_air": True}}}
    thread = Mock()
    thread.Thread.side_effect = lambda target, **kwargs: Mock(start=target)
    namespace = {"threading": thread, "state": state, "socketio": Mock()}
    for name in ["_refresh_robot_skill_profiles", "_start_telemetry_sync",
                 "_get_skill_catalog", "_get_system_status", "_start_airsim_camera_stream",
                 "_start_passive_perception", "_start_sensor_bridge"]:
        namespace[name] = Mock()
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    namespace["_try_connect_adapter"]()
    assert os.environ["SIM_ADAPTER"] == "mock"
    adapter = manager.get_primary_adapter()
    assert adapter.name == "mock" and adapter.is_connected()
    assert adapter.get_robot_snapshot()["UAV_1"]["position"] == [1, 2, -5]
    namespace["_refresh_robot_skill_profiles"].assert_called_once_with("mock")
    namespace["_start_telemetry_sync"].assert_called_once()
    namespace["_start_airsim_camera_stream"].assert_not_called()
    assert {call.args[0] for call in namespace["socketio"].emit.call_args_list} == {
        "world_state", "skill_catalog", "system_status"}
