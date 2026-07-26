"""Persist a fixed AirSim vehicle pool and activate a logical task fleet."""

from __future__ import annotations

import base64
import copy
import json
import os
import time
from typing import Iterable


DEFAULT_POOL_SIZE = 10
ACTIVATION_DROP_HEIGHT_M = 30.0
BOTTOM_DISTANCE_SENSOR = "BottomDistance"


class FleetSyncError(RuntimeError):
    pass


def _default_active_position(index: int) -> list[float]:
    group = (index - 1) // 2
    side = (index - 1) % 2
    return [
        10.0 + group * 30.0 + side * 10.0,
        -10.0 + side * 20.0,
        -10.0 - (index - 1) * 2.0,
    ]


def _parking_position(index: int) -> list[float]:
    """Place reserve UAVs outside the map and below the terrain."""
    row = (index - 1) // 5
    column = (index - 1) % 5
    return [
        500.0 + column * 8.0,
        500.0 + row * 8.0,
        80.0 + row * 5.0,
    ]


def _bottom_distance_sensor() -> dict:
    return {
        "SensorType": 5,
        "Enabled": True,
        "MinDistance": 0.2,
        "MaxDistance": 100.0,
        "X": 0.0,
        "Y": 0.0,
        "Z": 0.25,
        "Yaw": 0.0,
        "Pitch": -90.0,
        "Roll": 0.0,
        "DrawDebugPoints": False,
        "ExternalController": False,
    }


def _normalized_positions(count: int, positions: Iterable[dict]) -> list[dict]:
    by_index = {}
    for item in positions or []:
        robot_id = str(item.get("robot_id") or "")
        try:
            index = int(robot_id.rsplit("_", 1)[-1])
        except (TypeError, ValueError):
            continue
        raw = item.get("position") or _default_active_position(index)
        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
            continue
        by_index[index] = [float(raw[0]), float(raw[1]), float(raw[2])]

    return [
        {
            "robot_id": f"UAV_{index}",
            "vehicle": f"Drone_{index}",
            "position": by_index.get(index, _default_active_position(index)),
            "active": True,
        }
        for index in range(1, count + 1)
    ]


def _pool_layout(count: int, positions: Iterable[dict], pool_size: int) -> list[dict]:
    active = _normalized_positions(count, positions)
    layout = list(active)
    for index in range(count + 1, pool_size + 1):
        layout.append({
            "robot_id": f"UAV_{index}",
            "vehicle": f"Drone_{index}",
            "position": _parking_position(index),
            "active": False,
        })
    return layout


def _restart_command() -> str:
    override = os.getenv("AIRSIM_REMOTE_RESTART_COMMAND", "").strip()
    if override:
        return override
    task_name = os.getenv("AIRSIM_REMOTE_TASK_NAME", "AeroWeaver-AirSim")
    script = r"""
$ErrorActionPreference = 'Stop'
Stop-ScheduledTask -TaskName '__TASK_NAME__' -ErrorAction SilentlyContinue
Get-Process -Name 'VolnEnv*' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName '__TASK_NAME__'
Write-Output 'AIRSIM_RESTARTED'
""".replace("__TASK_NAME__", task_name.replace("'", "''")).strip()
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        "powershell.exe -NoLogo -NoProfile -NonInteractive "
        f"-ExecutionPolicy Bypass -EncodedCommand {encoded}"
    )


def _settings_targets(primary_path: str) -> list[str]:
    mirrors = os.getenv(
        "AIRSIM_REMOTE_SETTINGS_MIRRORS",
        "",
    )
    targets = [primary_path]
    targets.extend(path.strip() for path in mirrors.split(";") if path.strip())
    return list(dict.fromkeys(targets))


def synchronize_airsim_fleet(
    count: int,
    positions: Iterable[dict],
    force_restart: bool = False,
    pool_size: int = DEFAULT_POOL_SIZE,
) -> dict:
    """Persist the pool layout, restarting only when the physical pool changes."""
    try:
        import paramiko
    except ImportError as exc:
        raise FleetSyncError("Paramiko is required for AirSim fleet synchronization") from exc

    pool_size = max(1, min(int(pool_size), 12))
    count = max(1, min(int(count), pool_size))
    fleet = _normalized_positions(count, positions)
    pool = _pool_layout(count, positions, pool_size)
    host = os.getenv("AIRSIM_SSH_HOST", os.getenv("AIRSIM_HOST", "127.0.0.1"))
    port = int(os.getenv("AIRSIM_SSH_PORT", "22"))
    username = os.getenv("AIRSIM_SSH_USER", "")
    password = os.getenv("AIRSIM_SSH_PASSWORD", "")
    settings_path = os.getenv("AIRSIM_REMOTE_SETTINGS", "")
    if not username or not password or not settings_path:
        raise FleetSyncError(
            "AIRSIM_SSH_USER, AIRSIM_SSH_PASSWORD, and AIRSIM_REMOTE_SETTINGS "
            "are required for remote fleet resizing"
        )

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            host,
            port=port,
            username=username,
            password=password,
            timeout=10,
            auth_timeout=10,
        )
        sftp = ssh.open_sftp()
        try:
            with sftp.open(settings_path, "rb") as handle:
                original_bytes = handle.read()
            settings = json.loads(original_bytes.decode("utf-8-sig"))
            existing = settings.get("Vehicles") or {}
            template = existing.get("Drone_1")
            if not isinstance(template, dict):
                raise FleetSyncError("Drone_1 template is missing from AirSim settings")

            expected_names = [f"Drone_{index}" for index in range(1, pool_size + 1)]
            distance_sensor = _bottom_distance_sensor()
            vehicle_topology_changed = (
                sorted(existing, key=str.lower) != sorted(expected_names, key=str.lower)
            )
            sensor_topology_changed = any(
                (
                    (existing.get(vehicle_name) or {})
                    .get("Sensors", {})
                    .get(BOTTOM_DISTANCE_SENSOR)
                ) != distance_sensor
                for vehicle_name in expected_names
            )
            topology_changed = vehicle_topology_changed or sensor_topology_changed
            next_vehicles = {}
            for item in pool:
                vehicle_name = item["vehicle"]
                vehicle = copy.deepcopy(existing.get(vehicle_name) or template)
                vehicle["VehicleType"] = vehicle.get("VehicleType") or "SimpleFlight"
                spawn_position = list(item["position"])
                if item["active"]:
                    spawn_position[2] -= ACTIVATION_DROP_HEIGHT_M
                vehicle["X"], vehicle["Y"], vehicle["Z"] = spawn_position
                vehicle["AutoCreate"] = True
                vehicle["IsFpvVehicle"] = False
                vehicle.setdefault("Sensors", {})[BOTTOM_DISTANCE_SENSOR] = copy.deepcopy(
                    distance_sensor
                )
                next_vehicles[vehicle_name] = vehicle

            settings["Vehicles"] = next_vehicles
            settings["AeroWeaver"] = {
                "PoolSize": pool_size,
                "ActiveCount": count,
                "ParkingOrigin": _parking_position(1),
                "ActivationDropHeight": ACTIVATION_DROP_HEIGHT_M,
                "HoverClearance": 4.0,
                "BottomDistanceSensor": BOTTOM_DISTANCE_SENSOR,
            }
            payload = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            changed_paths = []
            backup_paths = []
            skipped_paths = []
            for target_path in _settings_targets(settings_path):
                try:
                    with sftp.open(target_path, "rb") as handle:
                        target_bytes = handle.read()
                except OSError:
                    skipped_paths.append(target_path)
                    continue
                try:
                    target_json = json.loads(target_bytes.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    target_json = None
                if target_json == settings:
                    continue
                backup_path = f"{target_path}.fleet-{timestamp}.bak"
                with sftp.open(backup_path, "wb") as handle:
                    handle.write(target_bytes)
                with sftp.open(target_path, "wb") as handle:
                    handle.write(payload)
                changed_paths.append(target_path)
                backup_paths.append(backup_path)
        finally:
            sftp.close()

        changed = bool(changed_paths)
        restart_output = ""
        restarted = bool(force_restart or topology_changed)
        if restarted:
            _, stdout, stderr = ssh.exec_command(_restart_command(), timeout=30)
            restart_output = stdout.read().decode("utf-8", errors="replace").strip()
            restart_error = stderr.read().decode("utf-8", errors="replace").strip()
            status = stdout.channel.recv_exit_status()
            if status != 0:
                raise FleetSyncError(restart_error or f"AirSim restart failed with status {status}")

        return {
            "ok": True,
            "changed": changed,
            "topology_changed": topology_changed,
            "restarted": restarted,
            "count": count,
            "pool_size": pool_size,
            "fleet": fleet,
            "pool": pool,
            "settings_path": settings_path,
            "settings_paths": _settings_targets(settings_path),
            "changed_paths": changed_paths,
            "backup_paths": backup_paths,
            "skipped_paths": skipped_paths,
            "restart_output": restart_output,
        }
    except FleetSyncError:
        raise
    except Exception as exc:
        raise FleetSyncError(str(exc)) from exc
    finally:
        ssh.close()
