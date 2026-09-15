"""Evaluate pinned MPE2 reward functions on the existing Mock task state.

No MPE environment, physics step, joint policy or second simulator is created.
These are MPE2 reward-function adaptations, not native MPE rollout rewards.
"""

import hashlib
import importlib
import importlib.metadata
from pathlib import Path
from types import SimpleNamespace

import numpy as np


SCENARIOS = {
    "coverage": "simple_spread", "pursuit": "simple_tag",
    "navigation": "simple_speaker_listener", "private_communication": "simple_crypto",
    "circle": "simple_formation", "line": "simple_line",
    "world_communication": "simple_world_comm", "collection": "collect_treasure",
    "concealment": "simple_adversary",
}


class Entity:
    # Identity equality is required by upstream collision rewards.
    def __init__(self, **values):
        self.__dict__.update(values)


class MockMPEReward:
    def __init__(self, task):
        version = importlib.metadata.version("mpe2")
        if version != "1.1.0":
            raise RuntimeError("Mock reward adapter requires audited mpe2==1.1.0")
        self.scenario_id = SCENARIOS[task.task_id]
        module = importlib.import_module(f"mpe2.{self.scenario_id}.{self.scenario_id}")
        self.scenario = module.Scenario()
        self.source = f"mpe2:{version}:{self.scenario_id}:mock_state_v1"
        bounds = task.bounds
        self.center = np.array([(bounds["north_min"] + bounds["north_max"]) / 2,
                                (bounds["east_min"] + bounds["east_max"]) / 2])
        self.scale = np.array([(bounds["north_max"] - bounds["north_min"]) / 2,
                               (bounds["east_max"] - bounds["east_min"]) / 2])
        if task.task_id == "circle":
            self.center = np.array(next(o["position"][:2] for o in task.objects if o["kind"] == "center"))
            radius = np.linalg.norm(np.array(next(o["position"][:2] for o in task.objects if o["kind"] == "slot")) - self.center)
            self.scale[:] = radius / self.scenario.TARGET_RADIUS
        elif task.task_id == "line":
            slots = [np.array(o["position"][:2]) for o in task.objects if o["kind"] == "slot"]
            self.center = (slots[0] + slots[-1]) / 2
            self.scale[:] = np.linalg.norm(slots[-1] - slots[0]) / self.scenario.TOTAL_SEP
            self.scenario._expected_positions = [self.position(p) for p in slots]
        self.manifest = {
            "reward_source": self.source, "native_mpe_rollout": False,
            "mpe2_version": version, "scenario_id": self.scenario_id,
            "source_sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
            "coordinate_center_m": self.center.tolist(), "coordinate_scale_m": self.scale.tolist(),
            "local_ratio": 0.5 if task.task_id == "coverage" else 0 if task.task_id in {"circle", "line"} else None,
            "state_mapping": "arena-normalized Mock state; formation geometry normalized to native target size",
            "variants": ["Mock physics, bounds, roles, horizon and completion criteria retained",
                         "communication symbols encoded as one-hot vectors; multiple listeners use mean native pair reward",
                         "collection uses current Mock cargo and availability; no native respawn or post_step"],
        }

    def position(self, value):
        return (np.array(value[:2], dtype=float) - self.center) / self.scale

    def world(self, task):
        world = SimpleNamespace(dim_p=2, dim_c=4, agents=[], landmarks=[], food=[])
        objects = {}
        for item in task.objects:
            entity = Entity(name=item["id"], state=SimpleNamespace(p_pos=self.position(item["position"])),
                            size=0.03, alive=not item.get("delivered", False) and not item.get("carried_by"),
                            type=0 if item.get("color") == "red" else 1)
            objects[item["id"]] = entity
            if item["kind"] in {"landmark", "center", "endpoint", "treasure"}:
                world.landmarks.append(entity)
            if item["kind"] == "treasure":
                entity.size = 0.025
            if item["kind"] == "food":
                entity.size = 0.03
                world.food.append(entity)
        agents = {}
        goal = objects.get(task.goal)
        for rid, role in task.roles.items():
            adversary = role in {"pursuer", "leader", "adversary", "eavesdropper"}
            size = 0.075 if adversary else 0.05
            if task.task_id == "coverage":
                size = 0.15
            elif task.task_id == "world_communication" and not adversary:
                size = 0.045
            elif task.task_id == "collection":
                size = 0.05 if role == "collector" else 0.075
            cargo = task.cargo.get(rid)
            agent = Entity(name=rid, adversary=adversary, collide=True, size=size,
                           speaker=role == "sender", collector=role == "collector",
                           holding=(0 if cargo["color"] == "red" else 1) if cargo else None,
                           d_i=0 if role == "deposit_red" else 1, goal_a=goal,
                           state=SimpleNamespace(p_pos=self.position(task.positions[rid]), c=np.zeros(4)))
            if rid in task.guesses:
                agent.state.c[int(task.guesses[rid])] = 1.0
            if task.task_id == "private_communication":
                agent.goal_a = SimpleNamespace(color=np.eye(4)[task.symbol])
            agents[rid] = agent
            world.agents.append(agent)
        if task.task_id == "navigation":
            speaker = agents[task.role_ids("speaker")[0]]
            world.agents = [speaker] + [a for a in world.agents if a is not speaker]
            speaker.goal_a = goal
        return world, agents

    def rewards(self, task):
        world, agents = self.world(task)
        if task.task_id == "navigation":
            # Native scenario has one speaker/listener pair; apply the same equation per listener.
            pair_rewards = {}
            for rid in task.role_ids("listener"):
                world.agents[0].goal_b = agents[rid]
                pair_rewards[rid] = float(self.scenario.reward(agents[rid], world))
            shared = float(np.mean(list(pair_rewards.values())))
            result = {rid: pair_rewards.get(rid, shared) for rid in agents}
        else:
            if task.task_id == "collection":
                self.scenario._reset_cached_rewards()
            ratio = self.manifest["local_ratio"]
            global_reward = float(self.scenario.global_reward(world)) if ratio is not None else 0.0
            result = {}
            for rid, agent in agents.items():
                local = float(self.scenario.reward(agent, world))
                result[rid] = local if ratio is None else ratio * local + (1 - ratio) * global_reward
        if not all(np.isfinite(value) for value in result.values()):
            raise ValueError("Non-finite environment reward")
        return result
