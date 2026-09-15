from types import SimpleNamespace

import pytest

from runtime.uav_agent_runtime import UAVAgentRuntime


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def dispatch_skill(self, step):
        self.calls.append(step)
        return SimpleNamespace(success=True, robot=step["robot"])


def test_agent_runtime_binds_dispatch_to_its_own_uav():
    delegate = FakeRuntime()
    runtime = UAVAgentRuntime(delegate, "UAV-2")

    result = runtime.dispatch_skill({"skill": "fly_to", "parameters": {"speed": 5}})

    assert result.success is True
    assert delegate.calls == [{
        "skill": "fly_to",
        "robot": "UAV_2",
        "parameters": {"speed": 5},
    }]


def test_agent_runtime_rejects_cross_uav_control():
    delegate = FakeRuntime()
    runtime = UAVAgentRuntime(delegate, "UAV_2")

    result = runtime.dispatch_skill({"skill": "fly_to", "robot": "UAV_3"})

    assert result.success is False
    assert "cannot control UAV_3" in result.error_msg
    assert delegate.calls == []


def test_commander_cannot_receive_a_physical_runtime():
    with pytest.raises(ValueError, match="physical UAV owner"):
        UAVAgentRuntime(FakeRuntime(), "COMMANDER")
