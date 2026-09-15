import os
import shutil
import subprocess
from pathlib import Path


def test_gazebo_guidance_scripts_are_present_and_syntax_valid():
    scripts = [
        Path("scripts/doctor_gazebo.sh"),
        Path("scripts/setup_px4.sh"),
        Path("scripts/start_sim.sh"),
        Path("scripts/sim_quickstart.sh"),
    ]
    for script in scripts:
        assert script.exists(), f"missing {script}"
        if os.name == "nt":
            continue
        assert script.stat().st_mode & 0o111, f"{script} should be executable"
        bash = shutil.which("bash")
        if not bash:
            continue
        result = subprocess.run(
            [bash, "-n", str(script)],
            check=False,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr


def test_gazebo_doctor_mentions_actionable_run_path():
    text = Path("scripts/doctor_gazebo.sh").read_text(encoding="utf-8")
    for expected in [
        "./scripts/setup_px4.sh",
        "./scripts/start_sim.sh",
        "SIM_ADAPTER=px4",
        "/api/sensor/status",
        "--live",
        "x500_lidar_2d_cam",
        "AeroWeaver modified UAV model",
    ]:
        assert expected in text


def test_sim_quickstart_handles_gazebo_python_bindings_without_polluting_venv():
    text = Path("scripts/sim_quickstart.sh").read_text(encoding="utf-8")
    assert "ensure_gazebo_python_path" in text
    assert "Do not add /opt/homebrew/lib/pythonX/site-packages" in text
    assert "GZ_PYTHONPATH" in text
    assert "gz.transport13" in text


def test_aeroweaver_modified_uav_model_is_required_for_showcase():
    model = Path("backend/sim/models/x500_lidar_2d_cam/model.sdf").read_text(encoding="utf-8")
    for expected in ["cam_front", "cam_rear", "cam_left", "cam_right", "cam_down", "lidar_2d"]:
        assert expected in model

    setup = Path("scripts/setup_px4.sh").read_text(encoding="utf-8")
    start = Path("scripts/start_sim.sh").read_text(encoding="utf-8")

    assert "AeroWeaver modified UAV model" in setup
    assert "PX4 Python build requirements" in setup
    assert "AeroWeaver macOS Clang warning compatibility patch" in setup
    assert "The full AeroWeaver research demo requires our modified UAV model" in start
