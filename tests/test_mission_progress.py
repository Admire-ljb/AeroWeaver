import math
from types import SimpleNamespace

import pytest

from brain.mission_progress import MissionProgressTracker, balance_movement_plan
from skills.scenario_skills import TeleportInitialize, scene_reset_window


def test_balance_movement_plan_gives_each_moving_agent_the_same_budget():
    plan_a = [
        {"skill": "fly_to", "parameters": {"target_position": [30, 0, -5]}},
    ]
    plan_b = [
        {"skill": "fly_relative", "parameters": {"forward": 3, "right": 4, "up": 0}},
        {"skill": "fly_relative", "parameters": {"forward": 0, "right": 5, "up": 0}},
    ]

    balanced_a, distance_a = balance_movement_plan(plan_a, [0, 0, -5], 12)
    balanced_b, distance_b = balance_movement_plan(plan_b, [0, 0, -5], 12)

    assert distance_a == distance_b == 12
    assert balanced_a[0]["parameters"]["target_position"] == [12.0, 0.0, -5.0]
    length_b = sum(
        math.sqrt(
            step["parameters"]["forward"] ** 2
            + step["parameters"]["right"] ** 2
            + step["parameters"]["up"] ** 2
        )
        for step in balanced_b
    )
    assert length_b == pytest.approx(12, abs=0.01)
    segment_total = sum(step["parameters"]["movement_segment_m"] for step in balanced_b)
    assert segment_total == pytest.approx(12, abs=0.01)



def test_world_step_increments_once_for_each_agent_decision():
    tracker = MissionProgressTracker()
    tracker.start(
        "mission-1",
        "Search an area",
        "Split the area",
        [
            {"robot_id": "UAV_1", "task": "west"},
            {"robot_id": "UAV_2", "task": "east"},
        ],
        [{"key": "steps", "measurement": "world_steps", "target": 2}],
        10,
    )

    tracker.record_decision("mission-1", "UAV_1")
    snapshot = tracker.record_decision("mission-1", "UAV_2")

    assert snapshot["world_step"] == 2
    assert [agent["decision_count"] for agent in snapshot["agents"]] == [1, 1]


def test_progress_metrics_use_actual_distribution_and_distance_balance():
    tracker = MissionProgressTracker()
    tracker.start(
        "mission-1",
        "Search an area",
        "Split the area",
        [
            {"robot_id": "UAV_1", "task": "west"},
            {"robot_id": "UAV_2", "task": "east"},
        ],
        [
            {"key": "separation", "measurement": "minimum_separation_m", "target": 5},
            {"key": "balance", "measurement": "distance_balance_pct", "target": 90},
        ],
        10,
    )
    tracker.record_result("mission-1", "UAV_1", True, 10)
    tracker.record_result("mission-1", "UAV_2", True, 9)
    tracker.record_termination_vote("mission-1", "UAV_1", True, "West sector complete")
    tracker.record_termination_vote("mission-1", "UAV_2", True, "East sector complete")
    snapshot = tracker.snapshot({
        "robots": {
            "UAV_1": {"position": [0, 0, -5]},
            "UAV_2": {"position": [8, 0, -5]},
        }
    }, active_links=1)
    metrics = {metric["key"]: metric["current"] for metric in snapshot["metrics"]}

    assert metrics["separation"] == 8
    assert metrics["balance"] == 90
    assert snapshot["status"] == "complete"
    assert snapshot["termination_consensus"] is True


def test_mission_stays_open_until_every_agent_votes_ready():
    tracker = MissionProgressTracker()
    tracker.start(
        "mission-1",
        "Search an area",
        "Split the area",
        [
            {"robot_id": "UAV_1", "task": "west"},
            {"robot_id": "UAV_2", "task": "east"},
        ],
        [],
        10,
        termination_conditions=[
            {"id": "coverage", "description": "Both sectors are covered."},
        ],
    )
    tracker.record_result("mission-1", "UAV_1", True, 10)
    tracker.record_result("mission-1", "UAV_2", True, 10)

    tracker.record_termination_vote("mission-1", "UAV_1", True, "West complete")
    waiting = tracker.record_termination_vote(
        "mission-1", "UAV_2", False, "One eastern strip remains"
    )

    assert waiting["status"] == "awaiting_consensus"
    assert waiting["termination_consensus"] is False
    assert waiting["agents"][1]["status"] == "continuing"

    completed = tracker.record_termination_vote(
        "mission-1", "UAV_2", True, "Eastern strip complete"
    )

    assert completed["status"] == "complete"
    assert completed["termination_consensus"] is True


def test_operator_cancel_is_terminal_and_blocks_late_agent_updates():
    tracker = MissionProgressTracker()
    tracker.start(
        "mission-1",
        "Search an area",
        "Split the area",
        [
            {"robot_id": "UAV_1", "task": "west"},
            {"robot_id": "UAV_2", "task": "east"},
        ],
        [],
        10,
    )
    tracker.record_decision("mission-1", "UAV_1")

    cancelled = tracker.cancel("mission-1", "Operator stopped the current mission.")
    tracker.record_result("mission-1", "UAV_1", True, 10, "late result")
    tracker.record_termination_vote("mission-1", "UAV_1", True, "late vote")
    final = tracker.snapshot()

    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_by"] == "operator"
    assert cancelled["termination_consensus"] is False
    assert all(agent["status"] == "cancelled" for agent in cancelled["agents"])
    assert final["status"] == "cancelled"
    assert final["agents"][0]["termination_vote"] is None


def test_world_step_timeout_is_terminal_and_preserves_evidence():
    tracker = MissionProgressTracker()
    tracker.start(
        "mission-1",
        "Pursuit",
        "Local pursuit",
        [{"robot_id": "UAV_1", "task": "capture"}],
        [],
        10,
        max_world_steps=4,
    )
    tracker.record_decision("mission-1", "UAV_1")
    timed_out = tracker.timeout(
        "mission-1",
        "Step limit reached before capture.",
        {"closest_distance_m": 12.5},
    )
    tracker.record_result("mission-1", "UAV_1", True, 10, "late result")

    final = tracker.snapshot()
    assert timed_out["status"] == "timeout"
    assert timed_out["termination_evidence"] == {"closest_distance_m": 12.5}
    assert final["status"] == "timeout"
    assert final["agents"][0]["status"] == "timeout"
    assert final["agents"][0]["moved_distance_m"] == 0


def test_claimed_report_phase_is_exposed_only_after_report_is_ready():
    tracker = MissionProgressTracker()
    tracker.start(
        "mission-1",
        "Search an area",
        "Split the area",
        [{"robot_id": "UAV_1", "task": "west"}],
        [],
        10,
        operator_report="Mission initialized",
    )

    assert tracker.claim_report_phase("mission-1", "final")
    claimed = tracker.snapshot()
    assert "final" in claimed["report_claims"]
    assert "final" not in claimed["report_phases"]
    assert claimed["latest_report"] == "Mission initialized"

    ready = tracker.set_report("mission-1", "final", "Final operational report")
    assert "final" in ready["report_phases"]
    assert ready["latest_report"] == "Final operational report"


def test_teleport_skill_is_rejected_outside_scene_reset(monkeypatch):
    calls = []

    class FakeAdapter:
        name = "fake"

        def reset_robot_pose(self, robot_id, position, *, in_air=True):
            calls.append((robot_id, position, in_air))
            return SimpleNamespace(
                success=True,
                message="initialized",
                data={"position": position},
                duration=0,
            )

    monkeypatch.setattr("adapters.adapter_manager.get_adapter", lambda: FakeAdapter())
    skill = TeleportInitialize()
    payload = {
        "robot_id": "UAV_1",
        "mission_id": "mission-1",
        "position": [1, 2, -5],
    }

    rejected = skill.execute(payload)
    with scene_reset_window("mission-1"):
        accepted = skill.execute(payload)

    assert not rejected.success
    assert accepted.success
    assert calls == [("UAV_1", [1.0, 2.0, -5.0], True)]
