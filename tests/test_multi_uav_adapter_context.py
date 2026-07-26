import threading
import unittest

from adapters import adapter_manager


class FakeAirSimAdapter:
    name = "airsim_openfly"

    def __init__(self, vehicle_name="Drone_1"):
        self._vehicle_name = vehicle_name
        self._airsim_host = "127.0.0.1"
        self._airsim_port = 41451
        self.connected = False
        self.active_robot = None

    @property
    def is_connected(self):
        return self.connected

    def connect(self, connection_str="", timeout=15.0):
        self.connected = True
        return True

    def invalidate_connection(self):
        self.connected = False

    def vehicle_for_robot(self, robot_id):
        index = int(str(robot_id).rsplit("_", 1)[1])
        return f"Drone_{index}"

    def set_active_robot(self, robot_id):
        self.active_robot = robot_id


class MultiUavAdapterContextTests(unittest.TestCase):
    def setUp(self):
        adapter_manager._close_robot_adapters()
        self.previous_adapter = adapter_manager._adapter
        self.previous_connection = adapter_manager._adapter_connection_str
        self.main = FakeAirSimAdapter()
        self.main.connected = True
        adapter_manager._adapter = self.main
        adapter_manager._adapter_connection_str = "127.0.0.1:41451"

    def tearDown(self):
        adapter_manager._close_robot_adapters()
        adapter_manager._adapter = self.previous_adapter
        adapter_manager._adapter_connection_str = self.previous_connection

    def test_each_execution_thread_gets_its_own_vehicle_adapter(self):
        barrier = threading.Barrier(2)
        results = {}

        def worker(robot_id):
            with adapter_manager.robot_adapter_context(robot_id):
                adapter = adapter_manager.get_adapter()
                barrier.wait(timeout=2)
                results[robot_id] = (
                    id(adapter),
                    adapter._vehicle_name,
                    adapter.active_robot,
                )

        threads = [
            threading.Thread(target=worker, args=("UAV_1",)),
            threading.Thread(target=worker, args=("UAV_2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(results["UAV_1"][1:], ("Drone_1", "UAV_1"))
        self.assertEqual(results["UAV_2"][1:], ("Drone_2", "UAV_2"))
        self.assertNotEqual(results["UAV_1"][0], results["UAV_2"][0])
        self.assertIs(adapter_manager.get_adapter(), self.main)

    def test_same_robot_reuses_its_isolated_adapter(self):
        with adapter_manager.robot_adapter_context("UAV_3"):
            first = adapter_manager.get_adapter()
        with adapter_manager.robot_adapter_context("UAV_3"):
            second = adapter_manager.get_adapter()

        self.assertIs(first, second)
        self.assertEqual(first._vehicle_name, "Drone_3")


if __name__ == "__main__":
    unittest.main()
