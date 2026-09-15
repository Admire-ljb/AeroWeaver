import math
import unittest

from adapters import adapter_manager
from adapters.mock_adapter import MockAdapter
from adapters.sim_adapter import ActionResult, Position
from skills.swarm_skills import (
    SwarmAreaSearch,
    SwarmOrbitHold,
    SwarmRendezvous,
    build_area_search_paths,
    build_formation_search_paths,
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

    def test_six_uavs_form_one_two_three_triangle_rows(self):
        offsets = formation_offsets(6, "triangle", 12.0)
        rows = {}
        for north, east in offsets:
            rows.setdefault(round(north, 6), []).append(east)

        self.assertEqual(sorted(len(columns) for columns in rows.values()), [1, 2, 3])
        self.assertGreaterEqual(
            minimum_separation([(north, east, -20.0) for north, east in offsets]),
            11.99,
        )

    def test_partial_triangle_rows_keep_requested_spacing(self):
        for count in range(2, 11):
            offsets = formation_offsets(count, "triangle", 12.0)
            self.assertGreaterEqual(
                minimum_separation([(north, east, -20.0) for north, east in offsets]),
                11.99,
                f"triangle spacing failed for {count} UAVs",
            )

    def test_formation_search_translates_one_rigid_triangle(self):
        paths, center_path, offsets = build_formation_search_paths(
            [f"UAV_{index}" for index in range(1, 7)],
            [30.0, 30.0, -15.0],
            100.0,
            80.0,
            tracks_per_uav=4,
            formation="triangle",
            spacing=12.0,
        )

        self.assertEqual(len(paths), 6)
        self.assertEqual(len(center_path), 8)
        robots = sorted(paths)
        for waypoint_index, center in enumerate(center_path):
            positions = [paths[robot_id][waypoint_index] for robot_id in robots]
            self.assertGreaterEqual(minimum_separation(positions), 11.99)
            self.assertAlmostEqual(
                sum(position[0] for position in positions) / len(positions),
                center[0],
                places=6,
            )
            self.assertAlmostEqual(
                sum(position[1] for position in positions) / len(positions),
                center[1],
                places=6,
            )
        self.assertEqual(len(offsets), 6)

    def test_circle_spacing_scales_for_larger_fleet(self):
        offsets = formation_offsets(8, "circle", 7.0)
        positions = [(north, east, -30.0) for north, east in offsets]
        self.assertGreaterEqual(minimum_separation(positions), 6.99)

    def test_area_search_paths_partition_the_rectangle(self):
        paths = build_area_search_paths(
            ["UAV_1", "UAV_2", "UAV_3"],
            [30.0, 10.0, -12.0],
            90.0,
            60.0,
            tracks_per_uav=4,
        )

        self.assertEqual(set(paths), {"UAV_1", "UAV_2", "UAV_3"})
        self.assertTrue(all(len(path) == 8 for path in paths.values()))
        self.assertTrue(all(min(point[0] for point in path) == 0.0 for path in paths.values()))
        self.assertTrue(all(max(point[0] for point in path) == 60.0 for path in paths.values()))
        self.assertLess(max(point[1] for point in paths["UAV_1"]), min(point[1] for point in paths["UAV_2"]))


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


class MockAreaSearchExecutionTests(unittest.TestCase):
    def setUp(self):
        adapter_manager._close_robot_adapters()
        self.previous_adapter = adapter_manager._adapter
        self.previous_connection = adapter_manager._adapter_connection_str
        mock = MockAdapter()
        mock.connect()
        mock.seed_fleet({
            "UAV_1": {"position": [0, 0, 0], "battery": 96},
            "UAV_2": {"position": [18, 0, 0], "battery": 93},
            "UAV_3": {"position": [18, 18, 0], "battery": 90},
            "UAV_4": {"position": [0, 18, 0], "battery": 87},
        })
        adapter_manager._adapter = mock
        adapter_manager._adapter_connection_str = "mock://"

    def tearDown(self):
        adapter_manager._close_robot_adapters()
        adapter_manager._adapter = self.previous_adapter
        adapter_manager._adapter_connection_str = self.previous_connection

    def test_six_uavs_preserve_triangle_during_area_search(self):
        adapter_manager._adapter.seed_fleet({
            "UAV_5": {"position": [36, 0, -12], "battery": 84, "in_air": True},
            "UAV_6": {"position": [36, 18, -12], "battery": 81, "in_air": True},
        })
        result = SwarmAreaSearch().execute({
            "robot_ids": "UAV_1,UAV_2,UAV_3,UAV_4,UAV_5,UAV_6",
            "area_center": [40, 40, -15],
            "area_width": 100,
            "area_height": 80,
            "speed": 20,
            "tracks_per_uav": 4,
            "formation": "triangle",
            "formation_spacing": 12,
            "visual_duration": 0.1,
        })

        self.assertTrue(result.success, result.error_msg)
        self.assertEqual(result.output["formation"], "triangle")
        self.assertIn(
            "\u4e09\u89d2\u5f62\u7f16\u961f",
            result.output["completion_summary_zh"].replace(" ", ""),
        )
        self.assertTrue(result.output["formation_preserved"])
        self.assertLessEqual(result.output["max_formation_error_m"], 0.001)
        self.assertEqual(len(result.output["search_paths"]), 6)
        self.assertGreaterEqual(
            minimum_separation(result.output["final_positions"].values()),
            11.99,
        )

    def test_four_uavs_visually_cover_one_area(self):
        result = SwarmAreaSearch().execute({
            "robot_ids": "UAV_1,UAV_2,UAV_3,UAV_4",
            "area_center": [30, 30, -12],
            "area_width": 80,
            "area_height": 60,
            "speed": 20,
            "tracks_per_uav": 4,
            "visual_duration": 0.1,
        })

        self.assertTrue(result.success, result.error_msg)
        self.assertEqual(result.output["coverage_percent"], 100.0)
        self.assertEqual(result.output["searched_area_m2"], 4800.0)
        self.assertEqual(len(result.output["search_paths"]), 4)
        snapshot = adapter_manager._adapter.get_robot_snapshot()
        self.assertTrue(all(item["in_air"] for item in snapshot.values()))
        self.assertTrue(all(not item["moving"] for item in snapshot.values()))
        self.assertEqual(len({tuple(item["position"]) for item in snapshot.values()}), 4)


if __name__ == "__main__":
    unittest.main()
