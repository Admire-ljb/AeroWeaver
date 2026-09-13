"""Role-indexed execution trajectories and reward-only skill preferences.

Legacy votes, mission summaries and synthetic success rewards are not learning
records. SQLite preserves raw trajectories and incrementally observed returns.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from memory.roles import infer_role


def _number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _tokens(value):
    text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "")
    return set(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.lower()))


def _overlap(left, right):
    a, b = _tokens(left), _tokens(right)
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class SkillCandidate:
    candidate_id: str
    skill: str
    robot_id: str
    parameters: dict = field(default_factory=dict)
    base_logprob: float = 0.0
    source: str = "llm"
    metadata: dict = field(default_factory=dict)
    role: str = ""

    @property
    def action_key(self):
        return self.candidate_id or f"{self.robot_id}:{self.skill}"

    @classmethod
    def from_dict(cls, value, index=0):
        return cls(
            candidate_id=str(value.get("candidate_id") or value.get("id") or f"C{index + 1}"),
            skill=str(value.get("skill") or ""),
            robot_id=str(value.get("robot_id") or value.get("robot") or "UAV_1"),
            parameters=dict(value.get("parameters") or {}),
            base_logprob=_number(value.get("base_logprob", value.get("logprob", 0.0))),
            source=str(value.get("source") or "llm"),
            metadata=dict(value.get("metadata") or {}),
            role=str(value.get("role") or value.get("agent_role") or ""),
        )


@dataclass
class ExperienceRecord:
    experience_id: str
    mission_id: str
    task: str
    state: Any
    action_id: str
    skill: str
    agent_id: str
    role: str
    step_index: int
    invocation: dict
    next_state: Any
    immediate_reward: float | None
    return_value: float | None
    reward_source: str | None
    trajectory_id: str = "main"
    gamma: float = 0.95
    success: bool = True
    base_logprob: float = 0.0
    trace: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    return_finalized: bool = False
    advantage: float = 0.0
    schema_version: int = 2


class SwarmExperienceMemory:
    def __init__(self, path=None, *, gamma=0.95, alpha=0.8,
                 agent_alpha=0.0, consensus_alpha=0.0, max_records=5000):
        requested = Path(path or os.getenv("AEROWEAVER_EXPERIENCE_PATH") or
                         Path(__file__).parent.parent / "data/swarm_experience/trajectories.sqlite3")
        # Do not reinterpret or overwrite pre-v2 JSONL rewards.
        self.path = requested if requested.suffix in {".sqlite", ".sqlite3", ".db"} else requested.with_suffix(".sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.gamma = float(gamma)
        if not math.isfinite(self.gamma) or not 0 <= self.gamma <= 1:
            raise ValueError("gamma must be in [0, 1]")
        self.alpha = float(alpha)
        self.max_records = max(1, int(max_records))
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS transitions (
                id TEXT PRIMARY KEY, mission TEXT NOT NULL, agent TEXT NOT NULL,
                trajectory TEXT NOT NULL, step INTEGER NOT NULL, reward REAL,
                return_value REAL, finalized INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                UNIQUE(mission, agent, trajectory, step)
            );
            CREATE INDEX IF NOT EXISTS trajectory_steps
                ON transitions(mission, agent, trajectory, step);
        """)

    def record_step(self, *, mission_id, task, state, action_id, skill, agent_id,
                    success, reward=None, base_logprob=0.0, trace=None, metadata=None,
                    role="", invocation=None, next_state=None, step_index=None,
                    trajectory_id="main", reward_source=None):
        if not mission_id or not agent_id or not skill:
            raise ValueError("Executed trajectory records require mission, agent and skill")
        metadata = dict(metadata or {})
        # Old callers pass executor success scores as reward without environment provenance.
        # Retain their execution evidence, but never treat these scores as task rewards.
        source = reward_source or metadata.get("reward_source")
        raw_reward = None
        if source and reward is not None:
            raw_reward = float(reward)
            if not math.isfinite(raw_reward):
                raise ValueError("Environment reward must be finite")
        with self._lock, self._db:
            key = (str(mission_id), str(agent_id), str(trajectory_id))
            previous = self._db.execute(
                "SELECT * FROM transitions WHERE mission=? AND agent=? AND trajectory=? ORDER BY step",
                key).fetchall()
            index = int(step_index) if step_index is not None else (previous[-1]["step"] + 1 if previous else 0)
            if previous and index <= previous[-1]["step"]:
                raise ValueError("Trajectory steps must be strictly increasing")
            if previous and previous[-1]["finalized"]:
                raise ValueError("Cannot append to a finalized trajectory")
            if previous:
                last = json.loads(previous[-1]["payload"])
                if last["gamma"] != self.gamma or last["reward_source"] != source:
                    raise ValueError("Discount or reward source changed within a trajectory")
            record = ExperienceRecord(
                experience_id=str(uuid.uuid4()), mission_id=key[0], agent_id=key[1],
                trajectory_id=key[2], task=str(task), state=state,
                action_id=str(action_id or skill), skill=str(skill),
                role=str(role).strip().lower() if role else infer_role(skill=skill, task=task, metadata=metadata),
                step_index=index, invocation=dict(invocation or metadata.get("parameters") or {}),
                next_state=next_state, immediate_reward=raw_reward, return_value=raw_reward,
                reward_source=source, gamma=self.gamma, success=bool(success),
                base_logprob=_number(base_logprob), trace=dict(trace or {}), metadata=metadata)
            self._db.execute(
                "INSERT INTO transitions(id,mission,agent,trajectory,step,reward,return_value,payload) VALUES(?,?,?,?,?,?,?,?)",
                (*[record.experience_id, *key, index, raw_reward, raw_reward],
                 json.dumps(asdict(record), ensure_ascii=False, allow_nan=False)))
            # Recompute only this participant's observed segment, never across the fleet.
            total = raw_reward
            next_index = index
            updates = []
            for row in reversed(previous):
                if row["reward"] is None or total is None:
                    total = None
                else:
                    total = row["reward"] + self.gamma ** (next_index - row["step"]) * total
                updates.append((total, row["id"]))
                next_index = row["step"]
            self._db.executemany("UPDATE transitions SET return_value=? WHERE id=?", updates)
            return record.experience_id

    def finalize_mission(self, *, mission_id, success, **ignored):
        with self._lock, self._db:
            self._db.execute("UPDATE transitions SET finalized=1 WHERE mission=?", (str(mission_id),))
            count = self._db.execute("SELECT COUNT(*) FROM transitions WHERE mission=?", (str(mission_id),)).fetchone()[0]
        return {"mission_id": str(mission_id), "success": bool(success), "records": count,
                "gamma": self.gamma, "reward_source": "environment_only"}

    # Compatibility hooks for progress monitoring. They no longer create memory records.
    def begin_mission(self, *args, **kwargs):
        pass

    def record_agent_result(self, **kwargs):
        pass

    def record_vote(self, **kwargs):
        pass

    def record_trace(self, *args, **kwargs):
        pass

    def events(self, mission_id=None):
        return []

    @staticmethod
    def _decode(row):
        payload = json.loads(row["payload"])
        payload.update(return_value=row["return_value"], return_finalized=bool(row["finalized"]))
        return ExperienceRecord(**payload)

    def records(self, mission_id=None):
        with self._lock:
            if mission_id is not None:
                rows = self._db.execute("SELECT * FROM transitions WHERE mission=? ORDER BY rowid", (str(mission_id),)).fetchall()
            else:
                rows = list(reversed(self._db.execute(
                    "SELECT * FROM transitions ORDER BY rowid DESC LIMIT ?", (self.max_records,)).fetchall()))
        return [self._decode(row) for row in rows]

    def retrieve(self, *, task, state, agent_id=None, role=None, top_k=12):
        role_filter = str(role).strip().lower() if role else ""
        matches = []
        for record in self.records():
            if record.return_value is None or not record.metadata.get("reuse_allowed", False):
                continue
            if record.task != str(task) or (role_filter and record.role != role_filter):
                continue
            score = _overlap(state, record.state)
            matches.append((score, record))
        matches.sort(key=lambda pair: (pair[0], pair[1].timestamp), reverse=True)
        return matches[:max(1, int(top_k))]

    def rank_candidates(self, *, task, state, candidates, agent_id=None, role=None, team_state=None):
        candidates = [c if isinstance(c, SkillCandidate) else SkillCandidate.from_dict(c, i)
                      for i, c in enumerate(candidates)]
        if not candidates:
            return {"selected": None, "ranking": [], "retrieved": 0}
        ranking, retrieved = [], 0
        for candidate in candidates:
            candidate_role = role or candidate.role or infer_role(skill=candidate.skill, task=task)
            records = [r for _, r in self.retrieve(task=task, state=state, role=candidate_role, top_k=32)]
            baseline = sum(r.return_value for r in records) / len(records) if records else 0.0
            same_skill = [r for r in records if r.skill == candidate.skill]
            advantage = (sum(r.return_value for r in same_skill) / len(same_skill) - baseline) if same_skill else 0.0
            prior = candidate.base_logprob
            ranking.append({"candidate": asdict(candidate), "candidate_id": candidate.action_key,
                            "role": candidate_role, "base_logprob": prior,
                            "corrected_logprob": prior + self.alpha * advantage, "advantage": advantage,
                            "retrieval_count": len(same_skill)})
            retrieved = max(retrieved, len(records))
        ranking.sort(key=lambda row: row["corrected_logprob"], reverse=True)
        return {"selected": ranking[0]["candidate"], "ranking": ranking, "retrieved": retrieved}

    def render_context(self, task, state, agent_id=None, role=None, top_k=4):
        matches = self.retrieve(task=task, state=state, role=role, top_k=top_k)
        return "\n".join(
            f"- {r.role}/{r.skill}: observed return={r.return_value:.4f}, match={score:.2f}"
            for score, r in matches) or "No shared swarm experience matched this state."

    def stats(self):
        with self._lock:
            total, missions, successes = self._db.execute(
                "SELECT COUNT(*),COUNT(DISTINCT mission),COALESCE(SUM(json_extract(payload,'$.success')),0) FROM transitions").fetchone()
            role_counts = self._db.execute(
                "SELECT json_extract(payload,'$.role'),COUNT(*) FROM transitions GROUP BY 1").fetchall()
        return {"records": total, "events": 0, "missions": missions, "roles": dict(role_counts),
                "success_rate": successes / total if total else 0.0, "path": str(self.path),
                "schema_version": 2, "gamma": self.gamma, "reward_source": "environment_only"}

    def export_mission(self, mission_id, path):
        with Path(path).open("w", encoding="utf-8") as handle:
            for record in self.records(mission_id):
                handle.write(json.dumps(asdict(record), ensure_ascii=False, allow_nan=False) + "\n")

    def clear(self):
        with self._lock, self._db:
            self._db.execute("DELETE FROM transitions")

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


__all__ = ["SkillCandidate", "ExperienceRecord", "SwarmExperienceMemory"]
