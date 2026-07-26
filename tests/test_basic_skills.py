import inspect
import time
import unittest
from unittest.mock import patch

from adapters.airsim_adapter import AirSimAdapter
from skills.cognitive_skills import (
    Alert,
    AskUser,
    HttpRequest,
    ReadFile,
    Report,
    RunPython,
    UpdateMap,
    WriteFile,
)
from skills.motor_skills import (
    ChangeAltitude,
    FlyRelative,
    FlyTo,
    GetBattery,
    GetMarks,
    GetPosition,
    Hover,
    Land,
    LookAround,
    MarkLocation,
    OrbitInspect,
    ReturnToLaunch,
    Takeoff,
)
from skills.perception_skills import (
    DetectObject,
    GetSensorData,
    Observe,
    Perceive,
    RecognizeSpeech,
)
from skills.swarm_skills import (
    SwarmFormationHold,
    SwarmOrbitHold,
    SwarmRendezvous,
)


BASIC_SKILLS = [
    Takeoff,
    Land,
    FlyTo,
    FlyRelative,
    Hover,
    ChangeAltitude,
    GetPosition,
    GetBattery,
    ReturnToLaunch,
    LookAround,
    MarkLocation,
    GetMarks,
    OrbitInspect,
    DetectObject,
    RecognizeSpeech,
    GetSensorData,
    Observe,
    Perceive,
    SwarmRendezvous,
    SwarmFormationHold,
    SwarmOrbitHold,
    RunPython,
    HttpRequest,
    ReadFile,
    WriteFile,
    Report,
    Alert,
    AskUser,
    UpdateMap,
]


class TerrainAdapter(AirSimAdapter):
    """Small deterministic AirSim adapter used to verify AGL safety logic."""

    def __init__(self):
        super().__init__("Drone_1")
        self._connected = True
        self._vehicle_names = ["Drone_1"]
        self._vehicle_spawn_poses = {"Drone_1": (0.0, 0.0, 0.0)}
        self._vehicle_home_positions = {}
        self._hold_running = True
        self._hold_x = 0.0
        self._hold_y = 0.0
        self._hold_z = -10.0
        self._landed = False
        self.ground_z = 0.0

    def get_ground_clearance(self, vehicle_name=None):
        return max(self.ground_z - self._hold_z, 0.01)

    def _do_set_pose(self, x, y, z):
        self._hold_x = float(x)
        self._hold_y = float(y)
        self._hold_z = float(z)

    def _read_global_pose(self, vehicle_name=None):
        return self._hold_x, self._hold_y, self._hold_z

    def _set_vehicle_global_pose(self, vehicle_name, x, y, z, yaw=0.0):
        self._hold_x = float(x)
        self._hold_y = float(y)
        self._hold_z = float(z)

    def _fly_smooth_raw(self, target_x, target_y, target_z, speed=5.0):
        self._do_set_pose(target_x, target_y, target_z)
        return "ok"


class BasicSkillContractTests(unittest.TestCase):
    def test_catalog_contains_29_unique_builtin_skills(self):
        names = [skill.name for skill in BASIC_SKILLS]
        self.assertEqual(len(names), 29)
        self.assertEqual(len(set(names)), 29)

    def test_basic_skills_do_not_expose_known_placeholder_results(self):
        for skill_class in BASIC_SKILLS:
            source = inspect.getsource(skill_class.execute)
            self.assertNotIn("暂不可用", source, skill_class.name)
            self.assertNotIn("raw_objects = [", source, skill_class.name)
            self.assertNotIn("搜索 A 区域并报告目标情况", source, skill_class.name)

    def test_run_python_allows_calculation_and_blocks_io(self):
        calculation = RunPython().execute({"code": "print(sum(range(10)))"})
        self.assertTrue(calculation.success)
        self.assertEqual(calculation.output["stdout"], "45\n")

        blocked = RunPython().execute({"code": "open('secret.txt').read()"})
        self.assertFalse(blocked.success)
        self.assertIn("approved calculation functions", blocked.error_msg)

    def test_browser_speech_transcript_is_not_fabricated(self):
        missing = RecognizeSpeech().execute({"language": "zh-CN"})
        self.assertFalse(missing.success)

        result = RecognizeSpeech().execute({
            "language": "zh-CN",
            "transcript": "飞往观察点",
        })
        self.assertTrue(result.success)
        self.assertEqual(result.output["text"], "飞往观察点")
        self.assertEqual(result.output["source"], "browser_speech_recognition")

    def test_battery_skill_reports_a_real_percentage_scale(self):
        class BatteryAdapter:
            name = "airsim_openfly"

            @staticmethod
            def get_battery():
                return 12.6, 1.0

        with patch("skills.motor_skills._get_adapter", return_value=BatteryAdapter()):
            result = GetBattery().execute({})
        self.assertTrue(result.success)
        self.assertEqual(result.output["remaining_percent"], 100.0)
        self.assertEqual(result.output["remaining_fraction"], 1.0)


class AirSimSafetyTests(unittest.TestCase):
    def setUp(self):
        self.adapter = TerrainAdapter()

    @patch("adapters.airsim_adapter.time.sleep", return_value=None)
    def test_takeoff_and_change_altitude_use_real_agl(self, _sleep):
        takeoff = self.adapter.takeoff(5.0)
        self.assertTrue(takeoff.success)
        self.assertAlmostEqual(self.adapter.get_ground_clearance(), 15.0)

        change = self.adapter.change_altitude(7.0)
        self.assertTrue(change.success)
        self.assertAlmostEqual(self.adapter.get_ground_clearance(), 7.0)

    @patch("adapters.airsim_adapter.time.sleep", return_value=None)
    def test_land_descends_to_surface_without_crossing_it(self, _sleep):
        result = self.adapter.land()
        self.assertTrue(result.success)
        self.assertGreaterEqual(self.adapter.get_ground_clearance(), 0.75)
        self.assertLessEqual(self.adapter.get_ground_clearance(), 0.9)
        self.assertLessEqual(self.adapter._hold_z, self.adapter.ground_z)

    @patch("adapters.airsim_adapter.time.sleep", return_value=None)
    def test_rotation_changes_heading_without_position_change(self, _sleep):
        before = self.adapter._xyz()
        result = self.adapter.rotate_by(90.0, duration=0.1)
        self.assertTrue(result.success)
        self.assertAlmostEqual(result.data["heading_deg"], 90.0)
        self.assertEqual(before, self.adapter._xyz())

    @patch("adapters.airsim_adapter.time.sleep", return_value=None)
    def test_fly_to_invalidates_stale_cockpit_position(self, _sleep):
        self.adapter._manual_states["Drone_1"] = {
            "x": -100.0,
            "y": -100.0,
            "z": -100.0,
            "yaw": 0.0,
            "last_seen": time.monotonic(),
        }

        result = self.adapter.fly_to_ned(5.0, 0.0, -10.0, speed=3.0)

        self.assertTrue(result.success)
        self.assertNotIn("Drone_1", self.adapter._manual_states)
        cockpit = self.adapter.set_velocity_body_for(
            "UAV_1",
            forward=1.0,
            right=0.0,
            down=0.0,
        )
        self.assertTrue(cockpit.success)
        self.assertGreater(self.adapter._hold_x, 5.0)
        self.assertGreater(self.adapter._hold_x, -50.0)

    @patch("adapters.airsim_adapter.time.sleep", return_value=None)
    def test_cockpit_command_interrupts_active_fly_to_owner(self, _sleep):
        self.adapter._autonomous_vehicle = "Drone_1"
        self.adapter.is_flying = True

        def stop_flight():
            self.adapter._stop_requested = True
            self.adapter.is_flying = False
            self.adapter._autonomous_vehicle = None

        self.adapter.request_stop = stop_flight
        result = self.adapter.set_velocity_body_for(
            "UAV_1",
            forward=1.0,
            right=0.0,
            down=0.0,
        )

        self.assertTrue(result.success)
        self.assertTrue(self.adapter._stop_requested)
        self.assertIsNone(self.adapter._autonomous_vehicle)


if __name__ == "__main__":
    unittest.main()
