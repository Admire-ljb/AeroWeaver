"""Thread-safe, per-UAV conversational context and visible message history."""

from __future__ import annotations

import json
import re
import threading
import time
from copy import deepcopy


def normalize_robot_id(robot_id: str) -> str:
    value = str(robot_id or "UAV_1").strip().upper().replace("-", "_")
    return value or "UAV_1"


def _extract_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.I)
    candidates = [fenced.group(1)] if fenced else []
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {}


def assess_termination(
    robot_id: str,
    assignment: str,
    termination_conditions: list[dict],
    execution_result: dict,
    own_state: dict,
    history: list[dict],
    llm_client,
) -> dict:
    """Ask one UAV agent whether its local evidence supports ending the mission."""
    robot_id = normalize_robot_id(robot_id)
    conditions = [
        str(item.get("description") or "").strip()
        for item in (termination_conditions or [])
        if isinstance(item, dict) and str(item.get("description") or "").strip()
    ]
    system_prompt = f"""You are {robot_id}, an autonomous UAV with an isolated context.
Decide whether the global mission may terminate from your local perspective.
You do not decide for peers and cannot see their private reasoning.
Vote READY only when your assignment is complete, the listed termination conditions are
supported by your own execution evidence, and you have no unresolved safety or coordination issue.
Otherwise vote CONTINUE and state exactly what remains.

Return JSON only:
{{
  "ready_to_end": true,
  "reason": "concise local judgment",
  "evidence": ["observable fact"],
  "unmet_conditions": []
}}
"""
    payload = {
        "robot_id": robot_id,
        "assignment": str(assignment or ""),
        "termination_conditions": conditions,
        "execution_result": execution_result or {},
        "own_state": own_state or {},
    }
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(
        {"role": item["role"], "content": item["content"]}
        for item in (history or [])[-10:]
        if item.get("role") in {"user", "assistant"} and item.get("content")
    )
    messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False)})
    raw = llm_client.chat(messages, temperature=0.2, max_tokens=500)
    parsed = _extract_json_object(raw)
    ready = parsed.get("ready_to_end") is True
    reason = str(parsed.get("reason") or "").strip()
    evidence = [str(item).strip() for item in (parsed.get("evidence") or []) if str(item).strip()]
    unmet = [str(item).strip() for item in (parsed.get("unmet_conditions") or []) if str(item).strip()]
    if not parsed:
        ready = False
        reason = "Termination assessment could not be parsed; continue the mission safely."
        unmet = conditions
    elif not reason:
        reason = "Local termination conditions are satisfied." if ready else "Local work remains."
    return {
        "ready_to_end": ready,
        "reason": reason,
        "evidence": evidence[:6],
        "unmet_conditions": unmet[:6],
    }


class UAVAgentContextStore:
    """Keeps LLM history isolated while exposing an auditable message stream."""

    def __init__(self, max_history: int = 40, max_messages: int = 500):
        self.max_history = max(4, int(max_history))
        self.max_messages = max(20, int(max_messages))
        self._contexts: dict[str, dict] = {}
        self._agent_locks: dict[str, threading.Lock] = {}
        self._messages: list[dict] = []
        self._links: dict[str, dict] = {}
        self._sequence = 0
        self._lock = threading.RLock()

    def ensure(self, robot_id: str) -> dict:
        robot_id = normalize_robot_id(robot_id)
        with self._lock:
            self._agent_locks.setdefault(robot_id, threading.Lock())
            context = self._contexts.setdefault(
                robot_id,
                {
                    "robot_id": robot_id,
                    "history": [],
                    "status": "idle",
                    "last_task": "",
                    "control_owner": "none" if robot_id == "COMMANDER" else robot_id,
                    "can_execute": robot_id != "COMMANDER",
                    "updated_at": time.time(),
                },
            )
            return deepcopy(context)

    def conversation_lock(self, robot_id: str) -> threading.Lock:
        robot_id = normalize_robot_id(robot_id)
        with self._lock:
            self.ensure(robot_id)
            return self._agent_locks[robot_id]

    def history(self, robot_id: str) -> list[dict]:
        robot_id = normalize_robot_id(robot_id)
        with self._lock:
            self.ensure(robot_id)
            return deepcopy(self._contexts[robot_id]["history"])

    def append_history(self, robot_id: str, role: str, content: str) -> None:
        robot_id = normalize_robot_id(robot_id)
        with self._lock:
            self.ensure(robot_id)
            history = self._contexts[robot_id]["history"]
            history.append({"role": str(role), "content": str(content)})
            self._contexts[robot_id]["history"] = history[-self.max_history :]
            self._contexts[robot_id]["updated_at"] = time.time()

    def update_task(self, robot_id: str, task: str, status: str) -> None:
        robot_id = normalize_robot_id(robot_id)
        with self._lock:
            self.ensure(robot_id)
            context = self._contexts[robot_id]
            context["last_task"] = str(task or context.get("last_task", ""))
            context["status"] = str(status or "idle")
            context["updated_at"] = time.time()

    def record_message(
        self,
        robot_id: str,
        sender: str,
        receiver: str,
        content: str,
        *,
        kind: str = "dialogue",
        intent: str = "CHAT",
    ) -> dict:
        robot_id = normalize_robot_id(robot_id)
        with self._lock:
            self.ensure(robot_id)
            self._sequence += 1
            event = {
                "id": self._sequence,
                "ts": round(time.time() * 1000),
                "robot_id": robot_id,
                "sender": str(sender),
                "receiver": str(receiver),
                "content": str(content),
                "kind": str(kind),
                "intent": str(intent),
            }
            self._messages.append(event)
            self._messages = self._messages[-self.max_messages :]
            self._contexts[robot_id]["updated_at"] = time.time()
            return deepcopy(event)

    def messages(self, robot_id: str | None = None) -> list[dict]:
        normalized = normalize_robot_id(robot_id) if robot_id else None
        with self._lock:
            rows = self._messages
            if normalized:
                rows = [
                    item for item in rows
                    if normalized in {
                        item.get("robot_id"), item.get("sender"), item.get("receiver")
                    }
                ]
            return deepcopy(rows)

    def establish_links(self, robot_ids, mission_id: str) -> list[dict]:
        """Create an active peer-to-peer mesh for the current mission."""
        agents = sorted({normalize_robot_id(item) for item in robot_ids if item})
        now = time.time()
        with self._lock:
            active_keys = set()
            for index, source in enumerate(agents):
                for target in agents[index + 1 :]:
                    key = f"{source}|{target}"
                    active_keys.add(key)
                    self._links[key] = {
                        "id": key,
                        "source": source,
                        "target": target,
                        "status": "active",
                        "mission_id": str(mission_id),
                        "updated_at": now,
                    }
            for key, link in self._links.items():
                if key not in active_keys:
                    link["status"] = "inactive"
                    link["updated_at"] = now
            return deepcopy([self._links[key] for key in sorted(active_keys)])

    def set_local_links(self, pairs, mission_id: str) -> list[dict]:
        """Replace active links with the currently reachable local peer pairs."""
        now = time.time()
        normalized_pairs = set()
        for pair in pairs or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            source = normalize_robot_id(pair[0])
            target = normalize_robot_id(pair[1])
            if source == target:
                continue
            normalized_pairs.add(tuple(sorted((source, target))))
        with self._lock:
            active_keys = set()
            for source, target in sorted(normalized_pairs):
                key = f"{source}|{target}"
                active_keys.add(key)
                self._links[key] = {
                    "id": key,
                    "source": source,
                    "target": target,
                    "status": "active",
                    "mission_id": str(mission_id),
                    "updated_at": now,
                }
            for key, link in self._links.items():
                if key not in active_keys:
                    link["status"] = "inactive"
                    link["updated_at"] = now
            return deepcopy([self._links[key] for key in sorted(active_keys)])

    def touch_link(self, source: str, target: str, mission_id: str = "") -> dict | None:
        source = normalize_robot_id(source)
        target = normalize_robot_id(target)
        if source == target:
            return None
        first, second = sorted((source, target))
        key = f"{first}|{second}"
        with self._lock:
            link = self._links.setdefault(
                key,
                {
                    "id": key,
                    "source": first,
                    "target": second,
                    "status": "active",
                    "mission_id": str(mission_id),
                    "updated_at": time.time(),
                },
            )
            link["status"] = "active"
            if mission_id:
                link["mission_id"] = str(mission_id)
            link["updated_at"] = time.time()
            return deepcopy(link)

    def links(self) -> list[dict]:
        with self._lock:
            return deepcopy([self._links[key] for key in sorted(self._links)])

    def snapshot(self) -> dict:
        with self._lock:
            contexts = {
                robot_id: {
                    "robot_id": robot_id,
                    "status": context.get("status", "idle"),
                    "last_task": context.get("last_task", ""),
                    "control_owner": context.get("control_owner", "none"),
                    "can_execute": bool(context.get("can_execute", False)),
                    "history_size": len(context.get("history", [])),
                    "updated_at": context.get("updated_at", 0),
                }
                for robot_id, context in sorted(self._contexts.items())
            }
            return {"contexts": contexts, "messages": deepcopy(self._messages), "links": self.links()}
