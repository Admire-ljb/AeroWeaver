import math
import unittest

from adapters import adapter_manager
from adapters.sim_adapter import ActionResult, Position
from skills.swarm_skills import (
    SwarmOrbitHold,
    SwarmRendezvous,
    formation_offsets,
    minimum_separation,
)


class FakeSwarmAirSimAdapter:
    name = "airsim_openfly"
    positions = {}

    def __init__(self, vehicle_name="Drone_1"):
        self._vehicle_name = vehicle_name
        self._vehicle_names = ["Drone_1", "Drone_2", "Drone_3"]
        self._pool_active_robots = {"UAV_1", "UAV_2", "UAV_3"}
        self._airsim_host = "127.0.0.1"
        self._airsim_port = 41451
        self.active_robot = f"UAV_{vehicle_name.rsplit('_', 1)[1]}"
        self.connected = False
        self.stopped = False

    @property
    def is_connected(self):
        return self.connected

    def connect(self, connection_str="", timeout=15.0):
        self.connected = True
        return True

    def invalidate_connection(self):
        self.connected = False

    def vehicle_for_robot(self, robot_id):
        return f"Drone_{str(robot_id).rsplit('_', 1)[1]}"

    def set_active_robot(self, robot_id):
        self.active_robot = robot_id
        self._vehicle_name = self.vehicle_for_robot(robot_id)

    def get_motion_position(self):
        north, east, down = self.positions[self.active_robot]
        return Position(north=north, east=east, down=down)

    def get_position(self):
        return self.get_motion_position()

    def _move(self, north, east, down):
        if self.stopped:
            return ActionResult(success=False, message="stopped")
        self.positions[self.active_robot] = [float(north), float(east), float(down)]
        return ActionResult(success=True, message="ok")

    def fly_to_ned(self, north, east, down, speed=5.0):
        return self._move(north, east, down)

    def fly_formation_segment(self, north, east, down, speed=5.0):
        return self._move(north, east, down)

    def hover(self, duration=0.0):
        return ActionResult(success=True, message="hover")

    def request_stop(self):
        self.stopped = True


class SwarmPlannerTests(unittest.TestCase):
    def test_supported_formations_preserve_requested_spacing(self):
        for formation in ("triangle", "circle", "line", "v"):
            offsets = formation_offsets(3, formation, 10.0)
            positions = [(north, east, -20.0) for north, east in offsets]
            self.assertGreaterEqual(minimum_separation(positions), 9.99)
            self.assertAlmostEqual(sum(point[0] for point in offsets), 0.0, places=6)
            self.assertAlmostEqual(sum(point[1] for point in offsets), 0.0, places=6)

    def test_circle_spacing_scales_for_larger_fleet(self):
        offsets = formation_offsets(8, "circle", 7.0)
        positions = [(north, east, -30.0) for north, east in offsets]
        self.assertGreaterEqual(minimum_separation(positions), 6.99)


class SwarmExecutionTests(unittest.TestCase):
    def setUp(self):
        adapter_manager._close_robot_adapters()
        self.previous_adapter = adapter_manager._adapter
        self.previous_connection = adapter_manager._adapter_connection_str
        FakeSwarmAirSimAdapter.positions = {
            "UAV_1": [0.0, 0.0, -12.0],
            "UAV_2": [24.0, 0.0, -12.0],
            "UAV_3": [0.0, 24.0, -12.0],
        }
        main = FakeSwarmAirSimAdapter()
        main.connected = True
        adapter_manager._adapter = main
        adapter_manager._adapter_connection_str = "127.0.0.1:41451"

    def tearDown(self):
        adapter_manager._close_robot_adapters()
        adapter_manager._adapter = self.previous_adapter
        adapter_manager._adapter_connection_str = self.previous_connection

    def test_three_uavs_rendezvous_without_losing_separation(self):
        result = SwarmRendezvous().execute({
            "robot_ids": ["UAV_1", "UAV_2", "UAV_3"],
            "center_position": [40.0, 15.0, -25.0],
            "formation": "triangle",
            "spacing": 10.0,
            "speed": 6.0,
            "post_action": "hold",
        })

        self.assertTrue(result.success, result.error_msg)
        self.assertGreaterEqual(result.output["minimum_observed_separation_m"], 4.5)
        final_positions = list(result.output["final_positions"].values())
        self.assertGreaterEqual(minimum_separation(final_positions), 9.99)
        self.assertAlmostEqual(
            sum(position[0] for position in final_positions) / 3,
            40.0,
            places=2,
        )
        self.assertAlmostEqual(
            sum(position[1] for position in final_positions) / 3,
            15.0,
            places=2,
        )

    def test_rotating_hold_completes_as_rigid_formation(self):
        result = SwarmOrbitHold().execute({
            "robot_ids": "UAV_1,UAV_2,UAV_3",
            "center_position": [30.0, 20.0, -24.0],
            "spacing": 12.0,
            "speed": 6.0,
            "duration": 2.0,
            "angular_speed": 10.0,
        })

        self.assertTrue(result.success, result.error_msg)
        self.assertEqual(result.output["orbit_steps"], 4)
        self.assertTrue(math.isfinite(result.output["minimum_observed_separation_m"]))
        self.assertGreaterEqual(
            minimum_separation(result.output["final_positions"].values()),
            11.99,
        )
        self.assertLessEqual(result.output["max_slot_error_m"], 0.01)
        self.assertLessEqual(result.output["altitude_spread_m"], 0.01)


if __name__ == "__main__":
    unittest.main()
