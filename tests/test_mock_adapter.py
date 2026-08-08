from adapters.mock_adapter import MockAdapter


def test_mock_adapter_basic_flight_cycle():
    adapter = MockAdapter()
    assert adapter.connect()
    assert adapter.is_connected()

    takeoff = adapter.takeoff(altitude=12)
    assert takeoff.success
    assert adapter.is_armed()
    assert adapter.is_in_air()
    assert adapter.get_position().down == -12

    fly = adapter.fly_to_ned(10, 5, -12, speed=10)
    assert fly.success
    pos = adapter.get_position()
    assert (pos.north, pos.east, pos.down) == (10, 5, -12)

    land = adapter.land()
    assert land.success
    assert not adapter.is_armed()
    assert not adapter.is_in_air()
    assert adapter.get_position().down == 0


def test_mock_adapter_supports_cockpit_velocity_controls():
    adapter = MockAdapter()

    disconnected = adapter.stop_velocity()
    assert not disconnected.success

    assert adapter.connect()
    adapter.takeoff(altitude=5)

    move = adapter.set_velocity_body(2.0, -1.0, 0.5, yaw_rate=15.0)
    assert move.success
    assert move.data["velocity_body"] == [2.0, -1.0, 0.5, 15.0]
    pos = adapter.get_position()
    assert pos.north == 0.2
    assert pos.east == -0.1
    assert pos.down == -4.95
    assert adapter.get_state().velocity == [2.0, -1.0, 0.5]

    stop = adapter.stop_velocity()
    assert stop.success
    assert adapter.get_state().velocity == [0.0, 0.0, 0.0]


def test_mock_adapter_tracks_a_complete_fleet_independently():
    adapter = MockAdapter()
    adapter.connect()
    adapter.seed_fleet({
        "UAV_1": {"position": [0, 0, 0], "battery": 95},
        "UAV_2": {"position": [18, 0, 0], "battery": 90},
        "UAV_3": {"position": [18, 18, 0], "battery": 85},
    })

    adapter.set_robot_position("UAV_2", 25, 10, -12, moving=True, in_air=True)
    snapshot = adapter.get_robot_snapshot()

    assert snapshot["UAV_1"]["position"] == [0.0, 0.0, 0.0]
    assert snapshot["UAV_2"]["position"] == [25.0, 10.0, -12.0]
    assert snapshot["UAV_2"]["moving"] is True
    assert snapshot["UAV_3"]["battery"] == 0.85
