import math
import unittest

from adapters import adapter_manager
from adapters.mock_adapter import MockAdapter
from skills.mission_skills import (
    SwarmEscortRoute,
    SwarmPerimeterPatrol,
    SwarmRelayDeploy,
    SwarmWaypointInspection,
    assign_waypoint_inspection_paths,
    build_escort_paths,
    build_perimeter_patrol_paths,
    build_relay_positions,
)
from skills.swarm_skills import minimum_separation


class MissionPathPlannerTests(unittest.TestCase):
    def test_perimeter_paths_use_distinct_phases(self):
        paths = build_perimeter_patrol_paths(
            ["UAV_1", "UAV_2", "UAV_3", "UAV_4"],
            [30, 20, -15],
            100,
            80,
            patrol_laps=1,
        )

        self.assertEqual(len(paths), 4)
        self.assertEqual(len({path[0] for path in paths.values()}), 4)
        self.assertTrue(all(path[0] == path[-1] for path in paths.values()))
        self.assertTrue(all(len(path) == 25 for path in paths.values()))

    def test_waypoints_are_distributed_round_robin(self):
        assignments = assign_waypoint_inspection_paths(
            ["UAV_1", "UAV_2", "UAV_3"],
            [[0, 0, -10], [10, 0, -10], [20, 0, -10], [30, 0, -10]],
        )

        self.assertEqual(len(assignments["UAV_1"]), 2)
        self.assertEqual(len(assignments["UAV_2"]), 1)
        self.assertEqual(len(assignments["UAV_3"]), 1)

    def test_relay_positions_are_evenly_spaced(self):
        positions = build_relay_positions(
            ["UAV_1", "UAV_2", "UAV_3", "UAV_4"],
            [0, 0, -20],
            [100, 0, -20],
        )

        ordered = [positions[f"UAV_{index}"] for index in range(1, 5)]
        self.assertEqual([point[0] for point in ordered], [20.0, 40.0, 60.0, 80.0])
        self.assertAlmostEqual(minimum_separation(ordered), 20.0)

    def test_escort_paths_preserve_formation_spacing(self):
        paths = build_escort_paths(
            ["UAV_1", "UAV_2", "UAV_3"],
            [[0, 0, -20], [30, 20, -20], [70, 10, -20]],
            formation="v",
            spacing=12,
        )

        for route_index in range(3):
            positions = [path[route_index] for path in paths.values()]
            self.assertGreaterEqual(minimum_separation(positions), 11.99)


class MockMissionExecutionTests(unittest.TestCase):
    def setUp(self):
        adapter_manager._close_robot_adapters()
        self.previous_adapter = adapter_manager._adapter
        self.previous_connection = adapter_manager._adapter_connection_str
        mock = MockAdapter()
        mock.connect()
        mock.seed_fleet({
            "UAV_1": {"position": [0, 0, -12], "battery": 96},
            "UAV_2": {"position": [20, 0, -12], "battery": 93},
            "UAV_3": {"position": [20, 20, -12], "battery": 90},
            "UAV_4": {"position": [0, 20, -12], "battery": 87},
        })
        adapter_manager._adapter = mock
        adapter_manager._adapter_connection_str = "mock://"

    def tearDown(self):
        adapter_manager._close_robot_adapters()
        adapter_manager._adapter = self.previous_adapter
        adapter_manager._adapter_connection_str = self.previous_connection

    def assert_terminal_success(self, skill, params):
        result = skill.execute({**params, "visual_duration": 0.1})
        self.assertTrue(result.success, result.error_msg)
        self.assertTrue(skill.terminal_on_success)
        self.assertEqual(skill.skill_level, "advanced")
        self.assertIn("completion_summary", result.output)
        self.assertIn("completion_summary_zh", result.output)
        self.assertGreater(
            result.output["minimum_observed_separation_m"],
            2.49,
        )
        snapshot = adapter_manager._adapter.get_robot_snapshot()
        self.assertTrue(all(not snapshot[robot_id]["moving"] for robot_id in result.output["robots"]))
        return result

    def test_perimeter_patrol_animates_full_fleet(self):
        result = self.assert_terminal_success(
            SwarmPerimeterPatrol(),
            {
                "robot_ids": "UAV_1,UAV_2,UAV_3,UAV_4",
                "area_center": [60, 60, -18],
                "area_width": 100,
                "area_height": 80,
                "patrol_laps": 1,
                "speed": 20,
            },
        )
        self.assertEqual(len(result.output["patrol_paths"]), 4)
        self.assertEqual(result.output["perimeter_distance_m"], 360.0)

    def test_waypoint_inspection_assigns_all_points(self):
        result = self.assert_terminal_success(
            SwarmWaypointInspection(),
            {
                "robot_ids": "UAV_1,UAV_2,UAV_3,UAV_4",
                "inspection_points": [
                    [20, 30, -18],
                    [60, 30, -18],
                    [60, 70, -18],
                    [20, 70, -18],
                    [40, 50, -18],
                ],
                "speed": 18,
            },
        )
        self.assertEqual(result.output["points_inspected"], 5)

    def test_relay_deploy_builds_ordered_chain(self):
        result = self.assert_terminal_success(
            SwarmRelayDeploy(),
            {
                "robot_ids": "UAV_1,UAV_2,UAV_3,UAV_4",
                "start_position": [0, -20, -18],
                "end_position": [120, 60, -18],
                "min_spacing": 10,
                "speed": 18,
            },
        )
        self.assertGreater(result.output["relay_spacing_m"], 20.0)

    def test_escort_route_keeps_v_formation(self):
        result = self.assert_terminal_success(
            SwarmEscortRoute(),
            {
                "robot_ids": "UAV_1,UAV_2,UAV_3,UAV_4",
                "route": [
                    [30, 30, -20],
                    [70, 50, -20],
                    [110, 30, -20],
                ],
                "formation": "v",
                "spacing": 12,
                "speed": 18,
            },
        )
        self.assertEqual(result.output["formation"], "v")
        self.assertGreater(result.output["route_distance_m"], 80.0)


if __name__ == "__main__":
    unittest.main()
