"""Completed-episode native-reward memory for the MPE engineering pilot."""

import numpy as np


class MPEExperienceMemory:
    def __init__(self, capacity=2000, neighbors=8, gamma=0.95, sharing="role"):
        if sharing not in {"role", "individual", "role_agnostic"}:
            raise ValueError("Unknown memory sharing control")
        self.capacity, self.neighbors, self.gamma = capacity, neighbors, gamma
        self.sharing = sharing
        self.records = []

    def advantages(self, scenario, owner, observation, skills):
        eligible = [r for r in self.records if r["scenario"] == scenario and r["team"] == owner.team
                    and (self.sharing == "role_agnostic" or r["role"] == owner.role)
                    and (self.sharing != "individual" or r["agent_id"] == owner.agent_id)
                    and len(r["state"]) == len(observation)]
        eligible.sort(key=lambda r: float(np.linalg.norm(np.asarray(r["state"]) - observation)))
        neighborhood = eligible[:self.neighbors]
        result = {name: 0.0 for name in skills}
        if neighborhood:
            baseline = float(np.mean([r["return"] for r in neighborhood]))
            for name in result:
                matched = [r["return"] for r in neighborhood if r["skill"] == name]
                if matched:
                    result[name] = float(np.mean(matched)) - baseline
        return result

    def finish_episode(self, scenario, trace, specs):
        # Commit only after the episode: current teammate records never enter a query.
        additions = []
        for agent_id, owner in specs.items():
            total = 0.0
            records = []
            for step in reversed(trace):
                if agent_id not in step["rewards"]:
                    continue
                total = float(step["rewards"][agent_id]) + self.gamma * total
                records.append({"scenario": scenario, "team": owner.team, "role": owner.role,
                                "agent_id": agent_id, "state": step["observations"][agent_id],
                                "skill": step["invocations"][agent_id]["skill"],
                                "reward": step["rewards"][agent_id], "return": total})
            additions.extend(reversed(records))
        self.records = (self.records + additions)[-self.capacity:]
