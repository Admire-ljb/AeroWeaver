#!/usr/bin/env python3
"""End-to-end smoke test for the device protocol API.

The script expects a running AeroWeaver server and verifies:
  1. system init with SIM_ADAPTER=mock,
  2. generic device registration/state/sensor REST flow,
  3. mock UAV command execution through Socket.IO,
  4. world state exposed for the web UI.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import requests
import socketio


def wait_for(predicate, timeout: float, interval: float = 0.5, label: str = "condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise RuntimeError(f"Timed out waiting for {label}; last={last!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    session = requests.Session()

    print("[1/8] waiting for server /api/status")
    wait_for(lambda: session.get(f"{base}/api/status", timeout=2).ok, 30, label="server status")

    print("[2/8] initializing AeroWeaver")
    init_resp = session.post(f"{base}/api/init", timeout=5)
    init_resp.raise_for_status()

    def initialized():
        data = session.get(f"{base}/api/status", timeout=2).json()
        return data if data.get("initialized") else None

    status = wait_for(initialized, 60, label="initialized status")
    print("    initialized:", status)

    print("[3/8] registering generic DEVICE_PROTOCOL UAV")
    device_id = f"drone_protocol_{int(time.time())}"
    reg_payload: dict[str, Any] = {
        "device_id": device_id,
        "device_type": "UAV",
        "capabilities": ["fly", "camera", "lidar"],
        "sensors": ["gps", "imu", "camera_front", "lidar_2d"],
        "protocol": "http",
        "metadata": {"model": "Mock protocol UAV", "firmware": "demo"},
    }
    reg = session.post(f"{base}/api/device/register", json=reg_payload, timeout=5)
    reg.raise_for_status()
    token = reg.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    print("[4/8] posting state and sensor data")
    state_payload = {
        "timestamp": time.time(),
        "battery": 76.5,
        "position": {"north": 12.0, "east": -4.0, "down": -8.0},
        "status": "idle",
        "in_air": True,
        "armed": True,
        "errors": [],
    }
    session.post(f"{base}/api/device/{device_id}/state", json=state_payload, headers=headers, timeout=5).raise_for_status()
    sensor_payload = {
        "timestamp": time.time(),
        "sensor_type": "lidar",
        "sensor_id": "lidar_2d",
        "data": {"ranges": [1.2, 1.5, 2.3], "angle_min": -3.14159, "angle_max": 3.14159},
    }
    session.post(f"{base}/api/device/{device_id}/sensor", json=sensor_payload, headers=headers, timeout=5).raise_for_status()
    devices = session.get(f"{base}/api/devices", timeout=5).json()
    registered = {device["device_id"]: device for device in devices["devices"]}
    assert device_id in registered, devices
    assert registered[device_id]["latest_sensor"]["sensor_id"] == "lidar_2d", devices

    print("[5/8] verifying DEVICE_PROTOCOL WebSocket heartbeat/action channel")
    device_sio = socketio.Client(reconnection=False, request_timeout=5)
    device_connected: list[dict[str, Any]] = []
    heartbeat_acks: list[dict[str, Any]] = []
    device_actions: list[dict[str, Any]] = []

    @device_sio.on("device_connected")
    def on_device_connected(data):
        device_connected.append(data)

    @device_sio.on("heartbeat_ack")
    def on_heartbeat_ack(data):
        heartbeat_acks.append(data)

    @device_sio.on("device_action")
    def on_device_action(data):
        device_actions.append(data)
        device_sio.emit("action_result", {
            "action_id": data["action_id"],
            "device_id": device_id,
            "success": True,
            "message": f"mock device executed {data['action']}",
            "output": {"echo": data.get("params", {})},
            "cost_time": 0.01,
        })

    device_sio.connect(base, transports=["websocket"])
    device_sio.emit("device_connect", {"device_id": device_id, "token": token})
    connected_event = wait_for(lambda: device_connected[0] if device_connected else None, 10, label="device_connected")
    assert connected_event.get("ok") is True, connected_event
    device_sio.emit("heartbeat", {"device_id": device_id, "timestamp": time.time()})
    wait_for(lambda: heartbeat_acks[0] if heartbeat_acks else None, 10, label="heartbeat_ack")
    action_response = session.post(
        f"{base}/api/device/{device_id}/action",
        json={"action": "takeoff", "params": {"altitude": 2}, "timeout": 10},
        timeout=5,
    )
    action_response.raise_for_status()
    action_event = wait_for(lambda: device_actions[0] if device_actions else None, 10, label="device_action")
    assert action_event["action"] == "takeoff", action_event
    device_sio.disconnect()

    print("[6/8] executing mock UAV land/takeoff/fly_to through Socket.IO")
    sio = socketio.Client(reconnection=False, request_timeout=5)
    results: list[dict[str, Any]] = []
    worlds: list[dict[str, Any]] = []

    @sio.on("skill_result")
    def on_skill_result(data):
        results.append(data)

    @sio.on("world_state")
    def on_world_state(data):
        worlds.append(data)

    # Use WebSocket transport to exercise the same realtime channel the web UI uses.
    sio.connect(base, transports=["websocket"])

    def execute_skill(skill_name: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        results.clear()
        sio.emit("execute_skill", {"robot_id": "UAV_1", "skill_name": skill_name, "parameters": parameters or {}})

        def wanted_result():
            for result in results:
                if result.get("skill") == skill_name:
                    return result
            return None

        result = wait_for(wanted_result, 30, label=f"{skill_name} skill_result")
        assert result.get("ok") is True, result
        return result

    # Make the verifier repeatable even if a previous run left the mock UAV airborne.
    execute_skill("land")
    execute_skill("takeoff", {"altitude": 6})
    execute_skill("fly_to", {"target_position": [8, 4, -6], "speed": 15})

    print("[7/8] verifying world state for frontend display")

    def world_has_uav():
        world = session.get(f"{base}/api/world", timeout=2).json()
        uav = world.get("robots", {}).get("UAV_1")
        pos = uav.get("position") if uav else None
        if uav and uav.get("in_air") is True and pos and round(float(pos[0]), 1) == 8.0:
            return world
        return None

    world = wait_for(world_has_uav, 20, label="UAV_1 airborne world state")
    assert device_id in world.get("robots", {}), world

    print("[8/8] loading frontend HTML")
    page = session.get(f"{base}/", timeout=5)
    page.raise_for_status()
    assert "<html" in page.text.lower() or "<!doctype html" in page.text.lower()

    sio.disconnect()
    print("OK: DEVICE_PROTOCOL demo, mock UAV simulation, and frontend endpoint verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
