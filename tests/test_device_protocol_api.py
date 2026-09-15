import time

from server import app, state


def setup_function():
    with state._device_lock:
        state.devices.clear()
        state.device_tokens.clear()
        state.device_sids.clear()


def test_device_protocol_rest_flow():
    client = app.test_client()
    registration = {
        "device_id": "drone_01",
        "device_type": "UAV",
        "capabilities": ["fly", "camera", "lidar"],
        "sensors": ["gps", "imu", "camera_front", "lidar_2d"],
        "protocol": "http",
        "metadata": {"model": "Mock UAV"},
    }

    register_response = client.post("/api/device/register", json=registration)
    assert register_response.status_code == 201
    register_data = register_response.get_json()
    assert register_data["ok"] is True
    assert register_data["device_id"] == "drone_01"
    assert register_data["token"].startswith("aw_drone_01_")

    token = register_data["token"]
    headers = {"Authorization": f"Bearer {token}"}

    duplicate = client.post("/api/device/register", json=registration)
    assert duplicate.status_code == 409
    assert duplicate.get_json()["code"] == "DEVICE_ALREADY_EXISTS"

    state_payload = {
        "timestamp": time.time(),
        "battery": 75.5,
        "position": {"north": 10.5, "east": -3.2, "down": -5.0},
        "status": "idle",
        "in_air": True,
        "armed": True,
        "errors": [],
    }
    state_response = client.post("/api/device/drone_01/state", json=state_payload, headers=headers)
    assert state_response.status_code == 200
    assert state_response.get_json() == {"ok": True, "device_id": "drone_01"}

    sensor_payload = {
        "timestamp": time.time(),
        "sensor_type": "lidar",
        "sensor_id": "lidar_2d",
        "data": {"ranges": [1.2, 1.5], "angle_min": -3.14, "angle_max": 3.14},
    }
    sensor_response = client.post("/api/device/drone_01/sensor", json=sensor_payload, headers=headers)
    assert sensor_response.status_code == 200
    assert sensor_response.get_json() == {"ok": True, "device_id": "drone_01"}

    devices_response = client.get("/api/devices")
    assert devices_response.status_code == 200
    devices_data = devices_response.get_json()
    assert devices_data["ok"] is True
    assert devices_data["count"] == 1
    device = devices_data["devices"][0]
    assert device["device_id"] == "drone_01"
    assert device["state"]["position"] == state_payload["position"]
    assert device["latest_sensor"]["sensor_id"] == "lidar_2d"

    skills_response = client.get("/api/device/drone_01/skills")
    assert skills_response.status_code == 200
    skills = skills_response.get_json()["skills"]
    assert "takeoff" in skills["hard"]
    assert "observe" in skills["perception"]

    offline_action = client.post("/api/device/drone_01/action", json={"action": "takeoff"})
    assert offline_action.status_code == 503
    assert offline_action.get_json()["code"] == "DEVICE_OFFLINE"

    with state._device_lock:
        state.device_sids["drone_01"] = "test-sid"
    action_response = client.post("/api/device/drone_01/action", json={"action": "takeoff", "params": {"altitude": 2}})
    assert action_response.status_code == 200
    assert action_response.get_json()["action"] == "takeoff"

    delete_response = client.delete("/api/device/drone_01", headers=headers)
    assert delete_response.status_code == 200
    assert client.get("/api/devices").get_json()["count"] == 0


def test_device_protocol_requires_bearer_token():
    client = app.test_client()
    registration = {
        "device_id": "drone_02",
        "device_type": "UAV",
        "capabilities": ["fly"],
        "sensors": ["gps"],
        "protocol": "http",
    }
    assert client.post("/api/device/register", json=registration).status_code == 201

    response = client.post("/api/device/drone_02/state", json={"timestamp": time.time()})
    assert response.status_code == 401
    assert response.get_json()["code"] == "INVALID_TOKEN"
