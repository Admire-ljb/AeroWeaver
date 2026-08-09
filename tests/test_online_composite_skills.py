from pathlib import Path
from types import SimpleNamespace

import pytest

from skills.composite_skill import (
    OnlineCompositeSkill,
    configure_composite_dispatcher,
)
from skills.composite_skill_manager import (
    CompositeDefinitionError,
    CompositeSkillManager,
)
from skills.online_skill_bootstrap import _cross_offsets


def sample_definition():
    return {
        "name": "cross_search",
        "description": "Search an area while preserving a cross formation.",
        "robot_type": ["UAV"],
        "input_schema": {
            "robot_ids": "active UAV IDs",
            "area_center": "[N, E, D] center",
        },
        "defaults": {
            "robot_ids": ["UAV_1", "UAV_2", "UAV_3"],
            "area_center": [0, 0, -15],
        },
        "steps": [
            {
                "skill": "swarm_area_search",
                "robot": "$robot_id",
                "parameters": {
                    "robot_ids": "$input.robot_ids",
                    "area_center": "$input.area_center",
                    "formation": "cross",
                },
            }
        ],
        "terminal_on_success": True,
        "completion_summary": "Cross search complete.",
    }


def test_manager_persists_validated_definitions(tmp_path: Path):
    manager = CompositeSkillManager(tmp_path)
    created = manager.create_skill(
        sample_definition(),
        allowed_skills={"swarm_area_search"},
    )
    assert created["name"] == "cross_search"
    assert (tmp_path / "cross_search.json").exists()

    reloaded = CompositeSkillManager(tmp_path)
    assert reloaded.get_definition("cross_search")["steps"][0]["skill"] == "swarm_area_search"


def test_manager_rejects_unknown_component(tmp_path: Path):
    manager = CompositeSkillManager(tmp_path)
    with pytest.raises(CompositeDefinitionError, match="unavailable skill"):
        manager.create_skill(sample_definition(), allowed_skills={"fly_to"})


def test_composite_resolves_public_inputs_and_dispatches():
    calls = []

    def dispatch(step):
        calls.append(step)
        return SimpleNamespace(success=True, output={"done": True}, error_msg="", cost_time=0.1)

    configure_composite_dispatcher(dispatch)
    result = OnlineCompositeSkill(sample_definition()).execute({
        "robot_id": "UAV_2",
        "robot_ids": ["UAV_2", "UAV_4"],
        "area_center": [10, 20, -12],
    })

    assert result.success is True
    assert calls == [{
        "step": 1,
        "skill": "swarm_area_search",
        "robot": "UAV_2",
        "parameters": {
            "robot_ids": ["UAV_2", "UAV_4"],
            "area_center": [10, 20, -12],
            "formation": "cross",
        },
    }]
    assert result.output["online_composite"] is True


def test_composite_stops_after_failed_component():
    definition = sample_definition()
    definition["steps"].append({
        "skill": "report",
        "robot": "$robot_id",
        "parameters": {"message": "should not run"},
    })
    calls = []

    def dispatch(step):
        calls.append(step)
        return SimpleNamespace(success=False, output={}, error_msg="blocked", cost_time=0.1)

    configure_composite_dispatcher(dispatch)
    result = OnlineCompositeSkill(definition).execute({"robot_id": "UAV_1"})

    assert result.success is False
    assert len(calls) == 1
    assert "blocked" in result.error_msg


def test_six_uav_cross_offsets_are_centered_and_separated():
    offsets = _cross_offsets(6, 12)
    assert len(offsets) == 6
    assert sum(point[0] for point in offsets) == pytest.approx(0)
    assert sum(point[1] for point in offsets) == pytest.approx(0)
    distances = [
        ((offsets[left][0] - offsets[right][0]) ** 2 +
         (offsets[left][1] - offsets[right][1]) ** 2) ** 0.5
        for left in range(len(offsets))
        for right in range(left + 1, len(offsets))
    ]
    assert min(distances) >= 12

