"""Native MPE2 closed loop, isolated from the 3D mock and flight executors."""

from dataclasses import asdict
import hashlib
import json
import time
import numpy as np

from brain.mpe_policy import local_candidates
from skills.mpe_skills import AgentSpec, MPEAgentSkills, team_for
from sim.mpe_catalog import create_mpe2_environment, role_for_instance, PAPER_SCENARIOS


class MPERuntime:
    def __init__(self, scenario, seed=0, max_cycles=100, memory=None, beta=0.1):
        if scenario not in PAPER_SCENARIOS:
            raise ValueError("Select one of the nine paper scenarios")
        if not 1 <= max_cycles <= 1000 or seed < 0:
            raise ValueError("Invalid seed or horizon")
        self.scenario, self.seed, self.max_cycles = scenario, seed, max_cycles
        self.memory, self.beta = memory, float(beta)
        self.env = create_mpe2_environment(scenario, max_cycles=max_cycles, continuous_actions=False)
        self.observations, _ = self.env.reset(seed=seed)
        self.round = 0
        self.trace = []
        self.specs, self.skills, self.rng = {}, {}, {}
        world = self.env.unwrapped.world
        for i, (agent_id, body) in enumerate(zip(self.env.possible_agents, world.agents)):
            role = role_for_instance(scenario, agent_id, i)
            spec = AgentSpec(agent_id, role, team_for(scenario, role), body.movable, body.silent,
                             int(self.env.action_space(agent_id).n), int(world.dim_c), i)
            self.specs[agent_id] = spec
            self.skills[agent_id] = MPEAgentSkills(scenario, spec)
            self.rng[agent_id] = np.random.default_rng(np.random.SeedSequence([seed, i]))
        self.public_context = {
            "agent_count": len(self.specs), "landmark_count": len(world.landmarks),
            "agents": [{"agent_id": s.agent_id, "role": s.role, "team": s.team} for s in self.specs.values()],
            "food_indices": [i for i, x in enumerate(world.landmarks) if x in getattr(world, "food", [])],
            "forest_indices": [i for i, x in enumerate(world.landmarks) if x in getattr(world, "forests", [])],
            "treasure_types": len(getattr(world, "treasure_types", [])),
            "deposit_types": {a.name: int(a.d_i) for a in world.agents if hasattr(a, "d_i") and a.d_i is not None},
        }
        self.returns = {a: 0.0 for a in self.specs}

    @property
    def done(self):
        return not self.env.agents

    def step(self, invocations):
        if self.done:
            raise ValueError("Episode has finished")
        if set(invocations) != set(self.env.agents):
            raise ValueError("Exactly one own-body invocation per live participant is required")
        actions = {a: self.skills[a].dispatch(invocations[a]) for a in self.env.agents}
        before = {a: o.tolist() for a, o in self.observations.items()}
        observations, rewards, terminations, truncations, _ = self.env.step(actions)
        self.observations = observations
        self.round += 1
        record = {"round": self.round, "observations": before, "invocations": invocations,
                  "actions": actions, "rewards": {a: float(r) for a, r in rewards.items()},
                  "terminations": terminations, "truncations": truncations}
        self.trace.append(record)
        for agent, reward in rewards.items():
            self.returns[agent] += float(reward)
        if self.done and self.memory is not None:
            self.memory.finish_episode(self.scenario, self.trace, self.specs)
        return record

    def policy_step(self, policy="local_heuristic"):
        if policy not in {"local_heuristic", "random_skills", "reward_memory"}:
            raise ValueError("Unknown engineering policy; LLM inference is not simulated implicitly")
        invocations = {}
        started = time.monotonic()
        for agent in self.env.agents:
            spec = self.specs[agent]
            obs = self.observations[agent].copy()
            candidates = local_candidates(self.scenario, spec, obs, self.public_context)
            advantages = {c["skill"]: 0.0 for c in candidates}
            if policy == "reward_memory" and self.memory is not None:
                advantages = self.memory.advantages(self.scenario, spec, obs, advantages)
            if policy == "random_skills":
                index = int(self.rng[agent].integers(len(candidates)))
            else:
                index = max(range(len(candidates)), key=lambda i: candidates[i]["base_score"] + self.beta * advantages[candidates[i]["skill"]])
            chosen = dict(candidates[index])
            chosen.update(advantage=advantages[chosen["skill"]], score_source="fixed_local_heuristic_not_llm_logits")
            chosen["corrected_score"] = chosen["base_score"] + self.beta * chosen["advantage"]
            invocations[agent] = chosen
        record = self.step(invocations)
        record["decision_ms"] = (time.monotonic() - started) * 1000
        return record

    def snapshot(self):
        # Privileged visualization only. NEVER passed to local_candidates.
        world = self.env.unwrapped.world
        entities = []
        for body in world.entities:
            color = np.asarray(getattr(body, "color", [0.5, 0.5, 0.5])).tolist()
            entities.append({"id": body.name, "position": body.state.p_pos.tolist(),
                             "radius": float(body.size), "color": color,
                             "role": self.specs[body.name].role if body.name in self.specs else "landmark",
                             "alive": bool(getattr(body, "alive", True)),
                             "movable": bool(body.movable)})
        return {"scenario": self.scenario, "seed": self.seed, "round": self.round,
                "max_cycles": self.max_cycles, "done": self.done, "entities": entities,
                "returns": self.returns.copy(), "roles": {a: asdict(s) for a, s in self.specs.items()},
                "last_step": {k: v for k, v in self.trace[-1].items() if k != "observations"} if self.trace else None,
                "reward_source": "unmodified_mpe2", "visualization_only": True}

    def trace_hash(self):
        stable = [{k: v for k, v in row.items() if k != "decision_ms"} for row in self.trace]
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    def close(self):
        self.env.close()
