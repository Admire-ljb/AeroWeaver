"""Mock fleet preflight recovery for duplicated swarm coordinates."""

from __future__ import annotations


def install() -> None:
    from adapters.adapter_manager import get_primary_adapter
    from skills import swarm_skills

    if getattr(swarm_skills, "_mock_overlap_recovery_hook", False):
        return
    original_motion = swarm_skills.execute_swarm_motion

    def execute_swarm_motion(input_data, default_formation, default_post_action):
        adapter = get_primary_adapter()
        get_snapshot = getattr(adapter, "get_robot_snapshot", None) if adapter else None
        set_position = getattr(adapter, "set_robot_position", None) if adapter else None
        if (
            getattr(adapter, "name", "") == "mock"
            and callable(get_snapshot)
            and callable(set_position)
        ):
            robot_ids = swarm_skills.normalize_robot_ids(
                (input_data or {}).get("robot_ids"), adapter
            )
            snapshot = get_snapshot()
            positions = {
                robot_id: tuple(snapshot.get(robot_id, {}).get("position") or ())
                for robot_id in robot_ids
            }
            valid = all(len(position) >= 3 for position in positions.values())
            minimum = (
                swarm_skills.minimum_separation(positions.values())
                if valid and len(positions) > 1
                else float("inf")
            )
            if minimum < 2.0:
                spacing = max(float((input_data or {}).get("spacing", 10.0)), 6.0)
                center_n = sum(position[0] for position in positions.values()) / len(positions)
                center_e = sum(position[1] for position in positions.values()) / len(positions)
                center_d = min(position[2] for position in positions.values())
                offsets = swarm_skills.formation_offsets(
                    len(robot_ids),
                    str((input_data or {}).get("formation") or "cross"),
                    spacing,
                )
                for index, robot_id in enumerate(robot_ids):
                    set_position(
                        robot_id,
                        center_n + offsets[index][0],
                        center_e + offsets[index][1],
                        center_d,
                        velocity=[0.0, 0.0, 0.0],
                        moving=False,
                        in_air=True,
                    )
        return original_motion(input_data, default_formation, default_post_action)

    swarm_skills.execute_swarm_motion = execute_swarm_motion
    swarm_skills._mock_overlap_recovery_hook = True

