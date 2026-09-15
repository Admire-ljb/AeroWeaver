import json
import threading
from types import SimpleNamespace

from brain.agent_loop import (
    AGENT_SYSTEM_PROMPT,
    AgentLoop,
    _enforce_goal_action_constraints,
)
from skills.base_skill import SkillResult


class FakeWorldModel:
    def get_world_state(self):
        return {
            "robots": {
                "UAV_1": {
                    "position": [0.0, 0.0, -10.0],
                    "battery": 100,
                    "status": "hovering",
                    "in_air": True,
                }
            }
        }


class FakeRegistry:
    def __init__(self, terminal_on_success):
        self.skill = SimpleNamespace(terminal_on_success=terminal_on_success)
        self.status_updates = []

    def get_skill_catalog(self):
        return []

    def get_skill(self, name):
        return self.skill if name == "swarm_area_search" else None

    def update_execution_status(self, name, success):
        self.status_updates.append((name, success))


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def dispatch_skill(self, step):
        self.calls.append(step)
        return SkillResult(
            success=True,
            output={"completion_summary": "Area search complete."},
            cost_time=0.01,
        )


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return json.dumps({
            "thinking": "Search the requested area once.",
            "decision": "act",
            "action": {
                "skill": "swarm_area_search",
                "robot": "UAV_1",
                "parameters": {},
            },
            "reflection": None,
            "goal_progress": "Starting search.",
        })


def build_loop(*, stop_event=None):
    llm = FakeLLM()
    runtime = FakeRuntime()
    registry = FakeRegistry(terminal_on_success=True)
    completed = []
    loop = AgentLoop(
        goal="Search the area with all UAVs.",
        llm_client=llm,
        runtime=runtime,
        world_model=FakeWorldModel(),
        skill_registry=registry,
        max_iterations=50,
        on_complete=lambda success, summary: completed.append((success, summary)),
        stop_event=stop_event,
    )
    loop._update_memory = lambda success: None
    return loop, llm, runtime, completed


def test_terminal_skill_success_finishes_without_repeating():
    loop, llm, runtime, completed = build_loop()

    loop.run()

    assert llm.calls == 1
    assert len(runtime.calls) == 1
    assert loop.iteration == 1
    assert len(loop.action_history) == 1
    assert completed == [(True, "Area search complete.")]


def test_preexisting_stop_does_not_increment_reasoning_round():
    stop_event = threading.Event()
    stop_event.set()
    loop, llm, runtime, completed = build_loop(stop_event=stop_event)

    loop.run()

    assert llm.calls == 0
    assert runtime.calls == []
    assert loop.iteration == 0
    assert completed == [(False, "操作员中止")]

def test_explicit_search_formation_is_a_hard_constraint():
    assert "explicitly requested formation is a hard mission constraint" in AGENT_SYSTEM_PROMPT
    assert 'formation="triangle"' in AGENT_SYSTEM_PROMPT
    assert 'formation="coverage"' in AGENT_SYSTEM_PROMPT
    assert "verify formation_preserved" in AGENT_SYSTEM_PROMPT

def test_goal_constraint_restores_triangle_parameter_before_dispatch():
    parameters = _enforce_goal_action_constraints(
        "\u8981\u6c426\u67b6\u65e0\u4eba\u673a\u7ec4\u6210\u4e09\u89d2\u5f62\u9635\u5217\u6267\u884c\u641c\u7d22",
        "swarm_area_search",
        {"formation": "coverage", "area_width": 100},
    )

    assert parameters["formation"] == "triangle"
    assert parameters["formation_spacing"] == 12.0
    assert parameters["area_width"] == 100
