"""Task-level native MPE skill catalog and body-bound one-cycle executors.

These skills never call the 3D mock flight adapter. Targets are displacements
available to the calling policy; symbols use the environment's native channel.
"""

from dataclasses import dataclass, asdict
import math
from pathlib import Path

from skills.base_skill import Skill, SkillResult


# One task catalog is filtered by native role after environment reset.
TASK_SKILLS = {
    "simple_spread": {"searcher": ("cover_landmark", "avoid_collision", "coast")},
    "simple_tag": {
        "pursuer": ("pursue_target", "avoid_collision", "coast"),
        "evader": ("evade_pursuer", "return_in_bounds", "coast"),
    },
    "simple_speaker_listener": {
        "speaker": ("signal_goal",), "listener": ("navigate_to_landmark", "coast"),
    },
    "simple_crypto": {
        "sender": ("encode_message",), "receiver": ("decode_message",),
        "eavesdropper": ("guess_message",),
    },
    "simple_formation": {"formation_member": ("take_circle_slot", "coast")},
    "simple_line": {"formation_member": ("take_line_slot", "coast")},
    "simple_world_comm": {
        "leader_adversary": ("pursue_and_signal", "coast_and_signal"),
        "adversary": ("pursue_target", "follow_signal", "coast"),
        "good_agent": ("seek_food", "seek_cover", "evade_pursuer", "coast"),
    },
    "collect_treasure": {
        "collector": ("collect_treasure", "deliver_treasure", "coast"),
        "depositor": ("meet_collector", "coast"),
    },
    "simple_adversary": {
        "good_agent": ("approach_goal", "approach_decoy", "coast"),
        "adversary": ("approach_inferred_goal", "coast"),
    },
}
SIGNAL_ONLY = {"signal_goal", "encode_message", "decode_message", "guess_message"}
MOTION_SIGNAL = {"pursue_and_signal", "coast_and_signal"}
AWAY = {"evade_pursuer", "avoid_collision"}


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str
    team: str
    movable: bool
    silent: bool
    action_count: int
    symbols: int
    index: int


def team_for(scenario, role):
    if scenario == "simple_crypto":
        return "eavesdropper" if role == "eavesdropper" else "allies"
    if scenario in {"simple_tag", "simple_world_comm"}:
        return "pursuers" if role in {"pursuer", "adversary", "leader_adversary"} else "evaders"
    if scenario == "simple_adversary":
        return "adversary" if role == "adversary" else "allies"
    return "cooperative"


def direction_action(delta, velocity=(0.0, 0.0)):
    """Quantized local PD steering; zero means no force, not teleport/hover."""
    values = list(delta)
    speed = list(velocity)
    if len(values) != 2 or len(speed) != 2:
        raise ValueError("target_delta and velocity require two coordinates")
    if not all(math.isfinite(float(x)) for x in values + speed):
        raise ValueError("Motion parameters must be finite")
    force = [float(values[i]) - 0.5 * float(speed[i]) for i in range(2)]
    axis = max(range(2), key=lambda i: abs(force[i]))
    if abs(force[axis]) < 0.03:
        return 0
    return (2 if force[0] > 0 else 1) if axis == 0 else (4 if force[1] > 0 else 3)


class MPEActionSkill(Skill):
    skill_type = "hard"
    skill_level = "basic"
    robot_type = ["MPE_PARTICLE"]
    preconditions = []
    cost = 0.0
    output_schema = {"native_action": "one native discrete action for the bound participant"}

    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self.doc_path = str(Path(__file__).parent / "docs" / "mpe" / "skill.md")
        self.description = name.replace("_", " ") + "; own participant, one native world cycle."
        self.input_schema = {}
        if name not in SIGNAL_ONLY and not name.startswith("coast"):
            self.input_schema.update(target_delta="observed or locally derived [x,y] displacement", velocity="own [vx,vy]")
        if name in SIGNAL_ONLY | MOTION_SIGNAL:
            self.input_schema["symbol"] = f"native channel symbol, integer 0..{owner.symbols - 1}"

    def check_precondition(self, robot_state):
        return True

    def execute(self, input_data):
        try:
            if input_data.get("robot_id", self.owner.agent_id) != self.owner.agent_id:
                raise ValueError("MPE skills may only actuate their bound participant")
            motion = 0
            if self.name not in SIGNAL_ONLY and not self.name.startswith("coast"):
                if not self.owner.movable:
                    raise ValueError("This native role cannot move")
                delta = input_data["target_delta"]
                if self.name in AWAY:
                    delta = [-float(x) for x in delta]
                motion = direction_action(delta, input_data.get("velocity", [0, 0]))
            symbol = 0
            if self.name in SIGNAL_ONLY | MOTION_SIGNAL:
                symbol = input_data["symbol"]
                if isinstance(symbol, bool) or not isinstance(symbol, int) or not 0 <= symbol < self.owner.symbols:
                    raise ValueError("Invalid native communication symbol")
                if self.owner.silent:
                    raise ValueError("This native role has no communication action")
            action = motion + 5 * symbol if self.owner.movable else symbol
            if not 0 <= action < self.owner.action_count:
                raise ValueError("Action is outside the native action space")
            return SkillResult(success=True, output={"native_action": action, "agent_id": self.owner.agent_id})
        except (KeyError, ValueError, TypeError) as exc:
            return SkillResult(success=False, error_msg=str(exc))


class MPEAgentSkills:
    def __init__(self, scenario, owner):
        self.owner = owner
        self.task_catalog = tuple(dict.fromkeys(name for names in TASK_SKILLS[scenario].values() for name in names))
        self.skills = {name: MPEActionSkill(name, owner) for name in TASK_SKILLS[scenario][owner.role]}

    def dispatch(self, invocation):
        if invocation.get("agent_id", self.owner.agent_id) != self.owner.agent_id:
            raise ValueError("Cross-body MPE invocation")
        name = invocation["skill"]
        if name not in self.skills:
            raise ValueError(f"Skill {name} is not active for {self.owner.role}")
        parameters = dict(invocation.get("parameters", {}))
        parameters["robot_id"] = self.owner.agent_id
        result = self.skills[name].execute(parameters)
        if not result.success:
            raise ValueError(result.error_msg)
        return result.output["native_action"]

    def describe(self):
        return {"owner": asdict(self.owner), "task_catalog": self.task_catalog,
                "active_skills": [{"name": s.name, "description": s.description,
                                   "input_schema": s.input_schema} for s in self.skills.values()]}
