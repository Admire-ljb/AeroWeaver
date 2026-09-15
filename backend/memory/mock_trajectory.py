"""Record actual Mock execution intervals independently of policy adaptation."""

from copy import deepcopy
import time


class MockTrajectoryRecorder:
    def __init__(self, memory, task, rewards, *, experiment_id="online_mock", split="development"):
        self.memory, self.task, self.rewards = memory, task, rewards
        self.experiment_id, self.split = experiment_id, split
        self.active = {}
        self.opened = {}
        self.step = 0
        self.count = 0

    def selected(self, rid, choice, decision_state, result, error, source):
        self.active[rid] = {
            "choice": deepcopy(choice), "decision_state": deepcopy(decision_state),
            "success": bool(result.success), "output": deepcopy(result.output),
            "error": error, "selector_source": source, "selected_at": time.time(),
            "selection_step": self.step,
        }

    def open_interval(self, observations, *, message_offset=None):
        self.opened = {rid: {"state": deepcopy(observations[rid]), "time": time.time(),
                             "message_offset": len(self.task.messages) if message_offset is None else message_offset} for rid in self.active}

    def close_interval(self, next_observations, *, final=False):
        if not self.opened:
            return {}
        rewards = self.rewards.rewards(self.task)
        for rid, start in self.opened.items():
            active = self.active[rid]
            choice = active["choice"]
            role = self.task.roles[rid]
            self.memory.record_step(
                mission_id=self.task.mission_id, task=self.task.task_id,
                state=start["state"], next_state=deepcopy(next_observations[rid]),
                action_id=f"{rid}:{self.step}", skill=choice["skill"], agent_id=rid, role=role,
                success=active["success"], reward=rewards[rid], reward_source=self.rewards.source,
                invocation={"robot": rid, **deepcopy(choice)}, step_index=self.step,
                trace={**deepcopy(active), "interval_start": start["time"], "interval_end": time.time(),
                       "peer_messages": [deepcopy(m) for m in self.task.messages[start["message_offset"]:]
                                         if rid in {m["source"], m["destination"]}]},
                metadata={"scenario_id": self.rewards.scenario_id, "seed": self.task.seed,
                          "experiment_id": self.experiment_id, "split": self.split,
                          "condition": self.task.policy, "reuse_allowed": False,
                          "online_update": False, "native_mpe": False,
                          "reward_provenance": self.rewards.manifest,
                          "selection_step": active["selection_step"],
                          "continued_skill": active["selection_step"] != self.step,
                          "final_interval": final, "task_status": self.task.status})
            self.count += 1
        self.step += 1
        self.opened = {}
        return rewards

    def finish(self, directory):
        self.memory.finalize_mission(mission_id=self.task.mission_id, success=self.task.status == "complete")
        self.memory.export_mission(self.task.mission_id, directory / "experience.jsonl")
