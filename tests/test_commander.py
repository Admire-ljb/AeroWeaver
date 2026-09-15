import json

from brain.commander import _normalize_mission, handle_global_input


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.messages = None

    def chat(self, messages, **_kwargs):
        self.messages = messages
        return self.reply


def _robots():
    return {
        "UAV_1": {"position": [0, 0, -5], "battery": 98, "status": "idle"},
        "UAV_2": {"position": [10, 0, -5], "battery": 92, "status": "idle"},
        "UAV_3": {"position": [20, 0, -5], "battery": 87, "status": "idle"},
    }


def test_commander_fills_missing_assignments_for_every_active_uav():
    client = FakeClient(json.dumps({
        "type": "mission",
        "summary": "Search three separate sectors.",
        "assignments": [
            {
                "robot_id": "UAV_1",
                "task": "Search the western sector and report detections.",
                "coordination_peers": ["UAV_2", "UAV_99"],
            }
        ],
        "termination_conditions": [
            {"id": "coverage", "description": "Every assigned sector is covered."},
            {"id": "consensus", "description": "Every UAV votes READY."},
        ],
    }))

    result = handle_global_input(
        "Search the whole area.",
        [],
        client,
        _robots(),
        force_mission=True,
    )

    assert result["type"] == "mission"
    assert [item["robot_id"] for item in result["assignments"]] == ["UAV_1", "UAV_2", "UAV_3"]
    assert result["assignments"][0]["task"].startswith("Search the western sector and report detections.")
    assert result["assignments"][0]["coordination_peers"] == ["UAV_2"]
    assert all(item["task"] for item in result["assignments"])

    assert result["movement_budget_m"] == 15.0
    assert len(result["initialization"]) == 3
    assert len({tuple(item["position"]) for item in result["initialization"]}) == 3
    assert all("movement budget: 15.0 m" in item["task"] for item in result["assignments"])
    assert "no authority to control a UAV" in client.messages[0]["content"]
    assert "UAV agents choose their own actions" in client.messages[0]["content"]
    assert [item["id"] for item in result["termination_conditions"]] == ["coverage", "consensus"]

def test_commander_chat_mode_does_not_dispatch_uavs():
    client = FakeClient('{"type":"chat","reply":"There are three active UAV agents."}')

    result = handle_global_input(
        "How many UAVs are active?",
        [],
        client,
        _robots(),
        conversation_only=True,
    )

    assert result == {
        "type": "chat",
        "reply": "There are three active UAV agents.",
        "assignments": [],
    }
    assert "Conversation-only mode is active" in client.messages[0]["content"]


def test_commander_ignores_unknown_and_duplicate_robot_assignments():
    client = FakeClient(json.dumps({
        "type": "mission",
        "assignments": [
            {"robot_id": "UAV_2", "task": "Search north."},
            {"robot_id": "UAV-2", "task": "Duplicate assignment."},
            {"robot_id": "UAV_9", "task": "Unknown robot."},
        ],
    }))

    result = handle_global_input("Search the area.", [], client, _robots(), force_mission=True)
    by_robot = {item["robot_id"]: item for item in result["assignments"]}

    assert set(by_robot) == {"UAV_1", "UAV_2", "UAV_3"}
    assert by_robot["UAV_2"]["task"].startswith("Search north.")


def test_commander_uses_llm_structured_pursuit_parameters():
    client = FakeClient(json.dumps({
        "type": "mission",
        "scenario": {
            "type": "pursuit",
            "pursuers": ["UAV_1", "UAV_2", "UAV_3"],
            "evader": "UAV_4",
            "pursuer_speed_mps": 14,
            "speed_ratio_by_robot": {"UAV_4": 2.0},
            "speed_mps_by_robot": {"UAV_4": 28.0},
            "fast_tactical": True,
        },
        "assignments": [],
    }))
    robots = {
        **_robots(),
        "UAV_4": {"position": [30, 0, -5], "battery": 84, "status": "idle"},
    }

    result = handle_global_input("UAV1-3追逐UAV4 UAV4有两倍速度", [], client, robots)

    assert result["type"] == "mission"
    assert result["scenario"]["type"] == "pursuit"
    assert result["scenario"]["pursuers"] == ["UAV_1", "UAV_2", "UAV_3"]
    assert result["scenario"]["evader"] == "UAV_4"
    assert result["scenario"]["speed_mps_by_robot"]["UAV_4"] == 28.0
    assert result["scenario"]["evader_speed_mps"] == 28.0
    assert "speed_ratio_by_robot" in client.messages[0]["content"]
    assert [item["robot_id"] for item in result["assignments"]] == [
        "UAV_1", "UAV_2", "UAV_3", "UAV_4",
    ]
    assert {item["role"] for item in result["assignments"]} == {"pursuer", "evader"}
    assert result["max_world_steps"] == result["scenario"]["max_world_steps"]
    assert {item["id"] for item in result["termination_conditions"]} == {
        "capture", "step_timeout",
    }


def test_commander_preserves_structured_task_area_as_hard_scenario_constraint():
    client = FakeClient('{"type":"mission","assignments":[]}')
    bounds = {"north_min": -30, "north_max": 20, "east_min": 15, "east_max": 65}

    result = handle_global_input(
        "Search inside the selected area.",
        [],
        client,
        _robots(),
        force_mission=True,
        task_area=bounds,
    )

    assert result["task_area"] == bounds
    assert result["scenario"]["area_bounds"] == bounds
    for item in result["initialization"]:
        north, east, _down = item["position"]
        assert bounds["north_min"] <= north <= bounds["north_max"]
        assert bounds["east_min"] <= east <= bounds["east_max"]
    assert all("Hard mission boundary" in item["task"] for item in result["assignments"])

def test_commander_preserves_explicit_pursuit_initialization_pose():
    robots = {
        "UAV_1": {"position": [0, 0, -5], "battery": 98, "status": "idle"},
        "UAV_2": {"position": [10, 0, -5], "battery": 92, "status": "idle"},
        "UAV_3": {"position": [20, 0, -5], "battery": 87, "status": "idle"},
    }
    result = _normalize_mission(
        {
            "scenario": {
                "type": "pursuit",
                "pursuers": ["UAV_2", "UAV_3"],
                "evader": "UAV_1",
            },
            "assignments": [],
            "initialization": [
                {"robot_id": "UAV1", "position": [42, 7, -12]},
            ],
        },
        "UAV2-3 chase UAV1",
        robots,
    )
    positions = {item["robot_id"]: item["position"] for item in result["initialization"]}

    assert positions["UAV_1"] == [42.0, 7.0, -12.0]
    assert set(positions) == {"UAV_1", "UAV_2", "UAV_3"}
