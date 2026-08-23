"""Mission-level state, world-step accounting, and balanced UAV motion plans."""

from __future__ import annotations

import math
import threading
from copy import deepcopy


_MOVEMENT_SKILLS = {"fly_to", "fly_relative"}
_TERMINAL_STATUSES = {"complete", "partial", "cancelled", "timeout"}


def _position(value) -> list[float]:
    values = list(value or [0.0, 0.0, 0.0])
    values = (values + [0.0, 0.0, 0.0])[:3]
    return [float(item) for item in values]


def _distance(a, b) -> float:
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def balance_movement_plan(
    steps: list[dict],
    current_position,
    movement_budget_m: float,
) -> tuple[list[dict], float]:
    """Scale a UAV plan so its aggregate translational path matches the round budget."""
    plan = deepcopy([step for step in (steps or []) if isinstance(step, dict)])
    budget = min(max(float(movement_budget_m or 0.0), 0.0), 100.0)
    if not plan or budget <= 0.0:
        return plan, 0.0

    cursor = _position(current_position)
    segments = []
    total = 0.0
    for index, step in enumerate(plan):
        skill = str(step.get("skill") or "")
        parameters = step.setdefault("parameters", {})
        if skill == "fly_to":
            target = _position(parameters.get("target_position"))
            vector = [target[i] - cursor[i] for i in range(3)]
            length = math.sqrt(sum(value * value for value in vector))
            if length > 1e-6:
                segments.append((index, skill, vector, length))
                total += length
            cursor = target
        elif skill == "fly_relative":
            vector = [
                float(parameters.get("forward", 0.0)),
                float(parameters.get("right", 0.0)),
                -float(parameters.get("up", 0.0)),
            ]
            length = math.sqrt(sum(value * value for value in vector))
            if length > 1e-6:
                segments.append((index, skill, vector, length))
                total += length
            cursor = [cursor[i] + vector[i] for i in range(3)]

    if total <= 1e-6:
        return plan, 0.0

    scale = budget / total
    cursor = _position(current_position)
    segment_map = {index: (skill, vector, length) for index, skill, vector, length in segments}
    for index, step in enumerate(plan):
        parameters = step.setdefault("parameters", {})
        segment = segment_map.get(index)
        if not segment:
            continue
        skill, vector, length = segment
        scaled = [value * scale for value in vector]
        if skill == "fly_to":
            cursor = [cursor[i] + scaled[i] for i in range(3)]
            parameters["target_position"] = [round(value, 3) for value in cursor]
        else:
            parameters["forward"] = round(float(parameters.get("forward", 0.0)) * scale, 3)
            parameters["right"] = round(float(parameters.get("right", 0.0)) * scale, 3)
            parameters["up"] = round(float(parameters.get("up", 0.0)) * scale, 3)
            cursor = [cursor[i] + scaled[i] for i in range(3)]
        parameters["movement_budget_m"] = round(budget, 3)
        parameters["movement_segment_m"] = round(length * scale, 3)

    return plan, round(budget, 3)


class MissionProgressTracker:
    """Thread-safe structured progress shared by Commander and all UAV agents."""

    def __init__(self):
        self._lock = threading.RLock()
        self._mission: dict = {}

    def start(
        self,
        mission_id: str,
        description: str,
        strategy: str,
        assignments: list[dict],
        metrics: list[dict],
        movement_budget_m: float,
        termination_conditions: list[dict] | None = None,
        operator_report: str = "",
        max_world_steps: int = 0,
        scenario: dict | None = None,
    ) -> dict:
        agents = {}
        for assignment in assignments:
            robot_id = str(assignment.get("robot_id") or "")
            if not robot_id:
                continue
            agents[robot_id] = {
                "robot_id": robot_id,
                "task": str(assignment.get("task") or ""),
                "status": "initializing",
                "decision_count": 0,
                "planned_distance_m": 0.0,
                "moved_distance_m": 0.0,
                "success": None,
                "last_summary": "",
                "position": None,
                "termination_vote": None,
                "termination_reason": "",
                "termination_evidence": [],
                "unmet_conditions": [],
            }
        normalized_metrics = []
        for metric in (metrics or [])[:5]:
            if not isinstance(metric, dict):
                continue
            key = str(metric.get("key") or metric.get("measurement") or "").strip()
            if not key:
                continue
            normalized_metrics.append({
                "key": key,
                "label": str(metric.get("label") or key.replace("_", " ").title()),
                "unit": str(metric.get("unit") or ""),
                "target": metric.get("target"),
                "measurement": str(metric.get("measurement") or key),
                "current": metric.get("current", 0),
            })
        if not normalized_metrics:
            normalized_metrics = [
                {
                    "key": "mission_progress",
                    "label": "Mission progress",
                    "unit": "%",
                    "target": 100,
                    "measurement": "completion_pct",
                    "current": 0,
                },
                {
                    "key": "minimum_separation",
                    "label": "Minimum separation",
                    "unit": "m",
                    "target": 5,
                    "measurement": "minimum_separation_m",
                    "current": 0,
                },
                {
                    "key": "distance_balance",
                    "label": "Movement balance",
                    "unit": "%",
                    "target": 90,
                    "measurement": "distance_balance_pct",
                    "current": 100,
                },
            ]

        normalized_conditions = []
        for index, condition in enumerate(termination_conditions or []):
            if isinstance(condition, str):
                condition_id = f"condition_{index + 1}"
                description = condition.strip()
            elif isinstance(condition, dict):
                condition_id = str(condition.get("id") or f"condition_{index + 1}").strip()
                description = str(condition.get("description") or "").strip()
            else:
                continue
            if description:
                normalized = {"id": condition_id, "description": description}
                if isinstance(condition, dict):
                    for key in ("measurement", "operator", "target", "hard"):
                        if key in condition:
                            normalized[key] = condition[key]
                normalized_conditions.append(normalized)

        with self._lock:
            self._mission = {
                "mission_id": str(mission_id),
                "description": str(description),
                "strategy": str(strategy),
                "status": "initializing",
                "world_step": 0,
                "round_index": 0,
                "max_world_steps": max(0, int(max_world_steps or 0)),
                "movement_budget_m": round(float(movement_budget_m), 2),
                "scenario": deepcopy(scenario or {}),
                "agents": agents,
                "metrics": normalized_metrics,
                "termination_conditions": normalized_conditions,
                "termination_consensus": False,
                "latest_report": str(operator_report or ""),
                "report_phases": ["start"] if operator_report else [],
                "report_claims": [],
            }
            return self._snapshot_locked()

    def reset_for_scene(self, mission_id: str) -> dict:
        """Clear counters when the server begins a new guarded scene reset."""
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            self._mission["world_step"] = 0
            self._mission["round_index"] = 0
            self._mission["status"] = "initializing"
            self._mission["termination_consensus"] = False
            self._mission.pop("latest_evidence", None)
            for agent in self._mission.get("agents", {}).values():
                agent.update({
                    "status": "initializing",
                    "decision_count": 0,
                    "planned_distance_m": 0.0,
                    "moved_distance_m": 0.0,
                    "success": None,
                    "position": None,
                    "termination_vote": None,
                    "termination_reason": "",
                    "termination_evidence": [],
                    "unmet_conditions": [],
                })
            return self._snapshot_locked()

    def mission_id(self) -> str:
        with self._lock:
            return str(self._mission.get("mission_id") or "")

    def movement_budget(self, mission_id: str) -> float:
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return 0.0
            return float(self._mission.get("movement_budget_m") or 0.0)

    def set_status(self, mission_id: str, status: str) -> dict:
        with self._lock:
            if (
                str(mission_id) == self._mission.get("mission_id")
                and self._mission.get("status") not in _TERMINAL_STATUSES
            ):
                self._mission["status"] = str(status)
            return self._snapshot_locked()

    def cancel(
        self,
        mission_id: str,
        reason: str = "Stopped by the operator.",
        cancelled_by: str = "operator",
    ) -> dict:
        """Move the active mission to a terminal cancelled state."""
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES:
                return self._snapshot_locked()
            self._mission["status"] = "cancelled"
            self._mission["termination_consensus"] = False
            self._mission["cancel_reason"] = str(reason or "Stopped by the operator.")
            self._mission["cancelled_by"] = str(cancelled_by or "operator")
            self._mission["latest_report"] = self._mission["cancel_reason"]
            for agent in self._mission.get("agents", {}).values():
                agent["status"] = "cancelled"
                agent["termination_vote"] = None
                agent["termination_reason"] = self._mission["cancel_reason"]
            return self._snapshot_locked()

    def timeout(self, mission_id: str, reason: str, evidence: dict | None = None) -> dict:
        """Finish an active mission at its configured hard world-step limit."""
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES:
                return self._snapshot_locked()
            self._mission["status"] = "timeout"
            self._mission["termination_consensus"] = False
            self._mission["termination_reason"] = str(reason or "Mission step limit reached.")
            self._mission["termination_evidence"] = deepcopy(evidence or {})
            self._mission["latest_report"] = self._mission["termination_reason"]
            for agent in self._mission.get("agents", {}).values():
                agent["status"] = "timeout"
                agent["termination_vote"] = False
                agent["termination_reason"] = self._mission["termination_reason"]
                agent["unmet_conditions"] = ["Mission completion condition was not reached before timeout."]
            return self._snapshot_locked()

    def record_round(
        self,
        mission_id: str,
        round_index: int,
        evidence: dict | None = None,
    ) -> dict:
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES:
                return self._snapshot_locked()
            self._mission["round_index"] = max(
                int(self._mission.get("round_index") or 0),
                int(round_index or 0),
            )
            if evidence:
                self._mission["latest_evidence"] = deepcopy(evidence)
            return self._snapshot_locked()

    def record_decision(self, mission_id: str, robot_id: str) -> dict:
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES:
                return self._snapshot_locked()
            agent = self._mission.get("agents", {}).get(str(robot_id))
            if agent is not None:
                agent["decision_count"] += 1
                agent["status"] = "deciding"
                agent["termination_vote"] = None
                agent["termination_reason"] = ""
                agent["termination_evidence"] = []
                agent["unmet_conditions"] = []
                self._mission["world_step"] += 1
                self._mission["status"] = "planning"
            return self._snapshot_locked()

    def record_plan(self, mission_id: str, robot_id: str, planned_distance_m: float) -> dict:
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES:
                return self._snapshot_locked()
            agent = self._mission.get("agents", {}).get(str(robot_id))
            if agent is not None:
                agent["planned_distance_m"] = round(
                    float(agent.get("planned_distance_m") or 0.0)
                    + max(0.0, float(planned_distance_m or 0.0)),
                    2,
                )
                agent["status"] = "executing" if planned_distance_m else "reporting"
            self._mission["status"] = "executing"
            return self._snapshot_locked()

    def record_result(
        self,
        mission_id: str,
        robot_id: str,
        success: bool,
        moved_distance_m: float = 0.0,
        summary: str = "",
    ) -> dict:
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES:
                return self._snapshot_locked()
            agent = self._mission.get("agents", {}).get(str(robot_id))
            if agent is not None:
                agent["success"] = bool(success)
                agent["status"] = "assessing"
                agent["moved_distance_m"] = round(
                    float(agent.get("moved_distance_m") or 0.0)
                    + max(0.0, float(moved_distance_m or 0.0)),
                    2,
                )
                agent["last_summary"] = str(summary or "")
            results = [item.get("success") for item in self._mission.get("agents", {}).values()]
            if results and all(value is not None for value in results):
                self._mission["status"] = "awaiting_consensus"
            return self._snapshot_locked()

    def record_termination_vote(
        self,
        mission_id: str,
        robot_id: str,
        ready_to_end: bool,
        reason: str = "",
        evidence: list | None = None,
        unmet_conditions: list | None = None,
    ) -> dict:
        """Record one independent UAV vote and finish only on unanimous READY."""
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES:
                return self._snapshot_locked()
            agent = self._mission.get("agents", {}).get(str(robot_id))
            if agent is not None:
                agent["termination_vote"] = bool(ready_to_end)
                agent["termination_reason"] = str(reason or "")
                agent["termination_evidence"] = [str(item) for item in (evidence or [])][:6]
                agent["unmet_conditions"] = [str(item) for item in (unmet_conditions or [])][:6]
                agent["status"] = "ready" if ready_to_end else "continuing"

            agents = list(self._mission.get("agents", {}).values())
            votes = [item.get("termination_vote") for item in agents]
            unanimous = bool(votes) and all(vote is True for vote in votes)
            self._mission["termination_consensus"] = unanimous
            if unanimous:
                self._mission["status"] = (
                    "complete" if all(item.get("success") for item in agents) else "partial"
                )
            elif votes and all(vote is not None for vote in votes):
                self._mission["status"] = "awaiting_consensus"
            else:
                self._mission["status"] = "consensus_pending"
            return self._snapshot_locked()

    def set_report(self, mission_id: str, phase: str, report: str) -> dict:
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return self._snapshot_locked()
            if self._mission.get("status") == "cancelled":
                return self._snapshot_locked()
            if self._mission.get("status") in _TERMINAL_STATUSES and phase != "final":
                return self._snapshot_locked()
            if phase != "final" and "final" in self._mission.setdefault("report_phases", []):
                return self._snapshot_locked()

            self._mission["latest_report"] = str(report or "")
            phases = self._mission.setdefault("report_phases", [])
            if phase and phase not in phases:
                phases.append(phase)
            return self._snapshot_locked()

    def claim_report_phase(self, mission_id: str, phase: str) -> bool:
        with self._lock:
            if str(mission_id) != self._mission.get("mission_id"):
                return False
            if self._mission.get("status") == "cancelled":
                return False
            if self._mission.get("status") in _TERMINAL_STATUSES and phase != "final":
                return False
            claims = self._mission.setdefault("report_claims", [])
            completed = self._mission.setdefault("report_phases", [])
            if phase in claims or phase in completed:
                return False
            claims.append(phase)
            return True

    def snapshot(self, world_state: dict | None = None, active_links: int = 0) -> dict:
        with self._lock:
            if world_state:
                robots = world_state.get("robots", {})
                for robot_id, agent in self._mission.get("agents", {}).items():
                    robot = robots.get(robot_id, {})
                    if robot.get("position") is not None:
                        agent["position"] = _position(robot.get("position"))
            snapshot = self._snapshot_locked()
        return self._with_metric_values(snapshot, int(active_links))

    def _snapshot_locked(self) -> dict:
        if not self._mission:
            return {
                "mission_id": "",
                "status": "idle",
                "world_step": 0,
                "round_index": 0,
                "max_world_steps": 0,
                "agents": [],
                "metrics": [],
                "termination_conditions": [],
                "termination_consensus": False,
                "latest_report": "",
            }
        result = deepcopy(self._mission)
        result["agents"] = [
            result["agents"][robot_id]
            for robot_id in sorted(result.get("agents", {}))
        ]
        return result

    @staticmethod
    def _with_metric_values(snapshot: dict, active_links: int) -> dict:
        agents = snapshot.get("agents", [])
        total = len(agents)
        completed = sum(agent.get("termination_vote") is True for agent in agents)
        positions = [agent.get("position") for agent in agents if agent.get("position") is not None]
        minimum_separation = 0.0
        if len(positions) > 1:
            minimum_separation = min(
                _distance(positions[i], positions[j])
                for i in range(len(positions))
                for j in range(i + 1, len(positions))
            )
        distances = [
            float(agent.get("moved_distance_m") or agent.get("planned_distance_m") or 0.0)
            for agent in agents
        ]
        positive_distances = [value for value in distances if value > 1e-6]
        distance_balance = 100.0
        if len(positive_distances) > 1:
            distance_balance = 100.0 * min(positive_distances) / max(positive_distances)
        expected_links = total * (total - 1) // 2

        measurements = {
            "completion_pct": 100.0 * completed / total if total else 0.0,
            "coverage_pct": 100.0 * completed / total if total else 0.0,
            "active_uavs": total,
            "communication_links": active_links,
            "communication_health_pct": (
                100.0 * active_links / expected_links if expected_links else 100.0
            ),
            "minimum_separation_m": minimum_separation,
            "distance_balance_pct": distance_balance,
            "world_steps": snapshot.get("world_step", 0),
            "round_index": snapshot.get("round_index", 0),
            "remaining_world_steps": max(
                0,
                int(snapshot.get("max_world_steps") or 0) - int(snapshot.get("world_step") or 0),
            ),
        }
        scenario = snapshot.get("scenario") or {}
        if scenario.get("type") == "pursuit":
            by_robot = {
                agent.get("robot_id"): agent.get("position")
                for agent in agents
                if agent.get("position") is not None
            }
            evader = str(scenario.get("evader") or "")
            pursuers = [
                robot_id for robot_id in scenario.get("pursuers") or []
                if robot_id in by_robot
            ]
            if evader in by_robot and pursuers:
                capture_distance = min(
                    _distance(by_robot[robot_id], by_robot[evader])
                    for robot_id in pursuers
                )
                measurements["capture_distance_m"] = capture_distance
                snapshot["capture_distance_m"] = round(capture_distance, 2)
        for metric in snapshot.get("metrics", []):
            measurement = str(metric.get("measurement") or metric.get("key") or "")
            if measurement in measurements:
                metric["current"] = round(float(measurements[measurement]), 1)
        snapshot["distribution"] = [
            {
                "robot_id": agent.get("robot_id"),
                "position": agent.get("position"),
                "status": agent.get("status"),
                "moved_distance_m": agent.get("moved_distance_m", 0.0),
                "termination_vote": agent.get("termination_vote"),
            }
            for agent in agents
        ]
        snapshot["completed_agents"] = completed
        snapshot["total_agents"] = total
        snapshot["active_links"] = active_links
        snapshot["termination_ready_count"] = completed
        snapshot["termination_pending_count"] = total - completed
        return snapshot
