"""Paper task forms implemented on the existing MockAdapter, not native MPE.

This module defines scene objects, role-local information and task diagnostics.
It neither advances physics nor chooses a joint action for the fleet.
"""

from __future__ import annotations

import math
import random
import threading
from collections import Counter
from copy import deepcopy


TASKS = {
    "coverage": ("Coverage", "Landmark coverage with collision avoidance", ("searcher",) * 3),
    "pursuit": ("Pursuit-evasion", "Pursuers cooperatively capture an evader", ("pursuer",) * 3 + ("evader",)),
    "navigation": ("Guided navigation", "A stationary speaker guides a mobile listener", ("speaker", "listener")),
    "private_communication": ("Private communication", "Key-sharing sender and receiver versus an eavesdropper", ("sender", "receiver", "eavesdropper")),
    "circle": ("Circular formation", "Maintain distinct slots around a central landmark", ("formation_member",) * 5),
    "line": ("Line formation", "Maintain evenly spaced slots between two landmarks", ("formation_member",) * 5),
    "world_communication": ("World communication", "A mobile leader informs pursuers; foragers seek food and cover", ("leader", "pursuer", "pursuer", "forager", "forager")),
    "collection": ("Collection-delivery", "Collectors carry one treasure to a matching mobile deposit agent", ("collector", "collector", "deposit_red", "deposit_blue")),
    "concealment": ("Goal concealment", "Informed agents approach a private goal among public landmarks", ("informed", "informed", "adversary")),
}

# The tuple in TASKS is the reproducible default, not a hard-coded runtime
# fleet. These rules preserve the minimum structure of each task while allowing
# the repeatable team role to grow from a natural-language or API request.
TASK_ROLE_RULES = {
    "coverage": {"minimums": {"searcher": 1}, "scalable_role": "searcher"},
    "pursuit": {"minimums": {"pursuer": 1, "evader": 1}, "scalable_role": "pursuer"},
    "navigation": {"minimums": {"speaker": 1, "listener": 1}, "scalable_role": "listener"},
    "private_communication": {
        "minimums": {"sender": 1, "receiver": 1, "eavesdropper": 1},
        "scalable_role": "receiver",
    },
    "circle": {"minimums": {"formation_member": 2}, "scalable_role": "formation_member"},
    "line": {"minimums": {"formation_member": 2}, "scalable_role": "formation_member"},
    "world_communication": {
        "minimums": {"leader": 1, "pursuer": 1, "forager": 1},
        "scalable_role": "forager",
    },
    "collection": {
        "minimums": {"collector": 1, "deposit_red": 1, "deposit_blue": 1},
        "scalable_role": "collector",
    },
    "concealment": {"minimums": {"informed": 1, "adversary": 1}, "scalable_role": "informed"},
}

ROLE_SKILLS = {
    "searcher": ("cover_landmark",),
    "pursuer": ("pursue_target", "follow_peer"),
    "evader": ("evade_peer", "return_in_bounds"),
    "speaker": ("signal_goal",),
    "listener": ("follow_signal",),
    "sender": ("encode_message",),
    "receiver": ("decode_message",),
    "eavesdropper": ("guess_message",),
    "formation_member": ("take_formation_slot",),
    "leader": ("pursue_target", "signal_target"),
    "forager": ("seek_food", "seek_cover", "evade_peer"),
    "collector": ("collect_treasure", "deliver_treasure"),
    "deposit_red": ("meet_collector",),
    "deposit_blue": ("meet_collector",),
    "informed": ("approach_goal", "approach_decoy"),
    "adversary": ("infer_goal",),
}
STATIONARY = {"speaker", "sender", "receiver", "eavesdropper"}
SKILL_NAMES = frozenset(s for skills in ROLE_SKILLS.values() for s in skills) | {"hold_position", "explore_local"}
MAX_TASK_FLEET = 10


def catalog():
    return [{
        "id": key,
        "title": title,
        "objective": objective,
        "roles": list(roles),
        "role_defaults": dict(Counter(roles)),
        "minimum_roles": dict(TASK_ROLE_RULES[key]["minimums"]),
        "scalable_role": TASK_ROLE_RULES[key]["scalable_role"],
        "fleet_size": len(roles),
        "max_fleet_size": MAX_TASK_FLEET,
        "substrate": "mock_point_mass",
        "native_mpe": False,
    } for key, (title, objective, roles) in TASKS.items()]


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class MockTask:
    sensor_range = 45.0
    communication_range = 55.0
    bounds = {"north_min": -70.0, "north_max": 70.0, "east_min": -70.0, "east_max": 70.0}

    def __init__(self, task_id, seed=0, max_rounds=120, bounds=None, role_counts=None,
                 fleet_size=None, role_assignments=None):
        if task_id not in TASKS:
            raise ValueError("Unknown Mock task")
        self.task_id, self.seed = task_id, int(seed)
        self.title, self.objective, default_roles = TASKS[task_id]
        if role_assignments is not None:
            roles = self._resolve_role_assignments(task_id, role_assignments)
        else:
            resolved = self._resolve_roles(task_id, role_counts=role_counts, fleet_size=fleet_size)
            roles = {f"UAV_{i + 1}": role for i, role in enumerate(resolved)}
        self.requested_role_counts = dict(role_counts or {})
        self.requested_fleet_size = int(fleet_size) if fleet_size is not None else None
        self.roles = dict(roles)
        self.requested_role_assignments = dict(self.roles) if role_assignments is not None else {}
        self.parser_source = "api"
        self.max_rounds = int(max_rounds)
        normalized_bounds = self._normalize_bounds(bounds)
        self.bounds = normalized_bounds or dict(type(self).bounds)
        self.custom_bounds = normalized_bounds is not None
        self.lock = threading.RLock()
        self.rng = random.Random(self.seed)
        self.positions = {rid: [-38 + i * 14, -34 + self.rng.uniform(-5, 5), -5.0]
                          for i, rid in enumerate(self.roles)}
        self.velocities = {rid: [0.0, 0.0, 0.0] for rid in self.roles}
        self.objects = []
        self.messages = []
        self.round = 0
        self.status = "ready"
        self.metrics = {}
        self.delivered = 0
        self.cargo = {rid: None for rid in self.roles}
        self.guesses = {}
        self.symbol = self.rng.randrange(4)
        self.key = self.rng.randrange(4)
        self.goal = f"landmark_{self.rng.randrange(3)}"
        self.stable_rounds = 0
        self.events = []
        self.motion_targets = {}
        self.evasion_states = {}
        self.motion_commands = {}
        self.pursuit_target_by_robot = {}
        self._build_objects()
        self._jitter_spawn_positions()
        self._jitter_scene_objects()
        self._fit_layout_to_bounds()
        self.active_skills = sorted({s for rid in self.roles for s in self.skills(rid)})

    @classmethod
    def _resolve_roles(cls, task_id, role_counts=None, fleet_size=None):
        """Build a task instance from defaults plus optional quantity overrides."""
        default_roles = TASKS[task_id][2]
        allowed = set(default_roles)
        counts = Counter(default_roles)
        if role_counts is not None and not isinstance(role_counts, dict):
            raise ValueError("role_counts must be an object mapping roles to non-negative integers")
        for role, raw_count in (role_counts or {}).items():
            if role not in allowed:
                continue
            try:
                count = int(raw_count)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid count for role {role}") from exc
            if count < 0:
                raise ValueError(f"Count for role {role} cannot be negative")
            counts[role] = count

        rule = TASK_ROLE_RULES[task_id]
        minimums = rule["minimums"]
        for role, minimum in minimums.items():
            counts[role] = max(counts.get(role, 0), minimum)

        if fleet_size is not None:
            try:
                target_size = int(fleet_size)
            except (TypeError, ValueError) as exc:
                raise ValueError("fleet_size must be an integer") from exc
            target_size = max(target_size, sum(minimums.values()))
            scalable_role = rule["scalable_role"]
            fixed_size = sum(count for role, count in counts.items() if role != scalable_role)
            counts[scalable_role] = max(minimums.get(scalable_role, 0), target_size - fixed_size)

        role_order = dict.fromkeys(default_roles)
        resolved = [role for role in role_order for _ in range(counts.get(role, 0))]
        if len(resolved) > MAX_TASK_FLEET:
            raise ValueError(f"Mock task supports at most {MAX_TASK_FLEET} UAVs in the current runtime")
        return resolved

    @classmethod
    def _resolve_role_assignments(cls, task_id, role_assignments):
        """Instantiate the exact contiguous roster returned by the semantic parser."""
        if isinstance(role_assignments, dict):
            role_assignments = [
                {"robot_id": robot_id, "role": role}
                for robot_id, role in role_assignments.items()
            ]
        if not isinstance(role_assignments, list) or not role_assignments:
            raise ValueError("role_assignments must be a non-empty list")
        allowed = set(TASKS[task_id][2])
        resolved = {}
        for item in role_assignments:
            if not isinstance(item, dict):
                raise ValueError("each role assignment must be an object")
            robot_id = str(item.get("robot_id") or "").strip().replace("-", "_").upper()
            if robot_id.startswith("UAV") and not robot_id.startswith("UAV_") and robot_id[3:].isdigit():
                robot_id = f"UAV_{int(robot_id[3:])}"
            role = str(item.get("role") or "").strip().lower()
            if not robot_id.startswith("UAV_") or not robot_id[4:].isdigit():
                raise ValueError("role_assignments contains an invalid UAV identifier")
            if role not in allowed:
                raise ValueError(f"role {role!r} is not supported by {task_id}")
            if robot_id in resolved:
                raise ValueError(f"duplicate role assignment for {robot_id}")
            resolved[robot_id] = role
        if len(resolved) > MAX_TASK_FLEET:
            raise ValueError(f"Mock task supports at most {MAX_TASK_FLEET} UAVs in the current runtime")
        expected = [f"UAV_{index}" for index in range(1, len(resolved) + 1)]
        if sorted(resolved, key=lambda rid: int(rid.split("_")[-1])) != expected:
            raise ValueError("role_assignments must use a contiguous UAV_1..UAV_N roster")
        counts = Counter(resolved.values())
        for role, minimum in TASK_ROLE_RULES[task_id]["minimums"].items():
            if counts[role] < minimum:
                raise ValueError(f"{task_id} requires at least {minimum} {role} role(s)")
        return {robot_id: resolved[robot_id] for robot_id in expected}

    def role_ids(self, role):
        return [rid for rid, current_role in self.roles.items() if current_role == role]

    @staticmethod
    def _normalize_bounds(value):
        if not isinstance(value, dict):
            return None
        try:
            normalized = {
                key: float(value[key])
                for key in ("north_min", "north_max", "east_min", "east_max")
            }
        except (KeyError, TypeError, ValueError):
            return None
        if (normalized["north_max"] - normalized["north_min"] < 2.0
                or normalized["east_max"] - normalized["east_min"] < 2.0):
            return None
        return {key: round(number, 3) for key, number in normalized.items()}

    def _object(self, name, kind, n, e, **extra):
        self.objects.append({"id": name, "kind": kind, "position": [float(n), float(e), -5.0], **extra})

    def _build_objects(self):
        if self.task_id in {"coverage", "navigation", "concealment"}:
            for i, (n, e) in enumerate(((-28, 24), (4, 34), (32, 20))):
                self._object(f"landmark_{i}", "landmark", n, e)
        elif self.task_id == "circle":
            self._object("center", "center", 0, 12)
            for i, rid in enumerate(self.roles):
                angle = 2 * math.pi * i / len(self.roles)
                self._object(f"slot_{i}", "slot", 22 * math.cos(angle), 12 + 22 * math.sin(angle), owner=rid)
        elif self.task_id == "line":
            self._object("start", "endpoint", -35, 18)
            self._object("end", "endpoint", 35, 18)
            for i, rid in enumerate(self.roles):
                denominator = max(1, len(self.roles) - 1)
                self._object(f"slot_{i}", "slot", -28 + 56 * i / denominator, 18, owner=rid)
        elif self.task_id == "world_communication":
            for i, (n, e) in enumerate(((25, 25), (-20, 28))):
                self._object(f"food_{i}", "food", n, e, visits=0)
                self._object(f"forest_{i}", "forest", n - 10, e - 8, radius=10)
            for i, rid in enumerate(self.role_ids("forager")):
                anchor = ((25, 6), (-20, 10))[i % 2]
                self.positions[rid] = [anchor[0] + 7 * (i // 2), anchor[1] + 5 * (i // 2), -5]
        elif self.task_id == "collection":
            for i, (n, e) in enumerate(((-25, 0), (20, 4), (-8, 22), (30, 30))):
                self._object(f"treasure_{i}", "treasure", n, e, color="red" if i % 2 == 0 else "blue", carried_by=None, delivered=False)
            for i, rid in enumerate(self.role_ids("collector")):
                self.positions[rid] = [-22 + 12 * (i % 2), 34 + 7 * (i // 2), -5]
            for i, rid in enumerate(self.role_ids("deposit_red") + self.role_ids("deposit_blue")):
                self.positions[rid] = [26 + 10 * (i % 2), 38 + 7 * (i // 2), -5]
        elif self.task_id == "pursuit":
            for i, rid in enumerate(self.role_ids("evader")):
                self.positions[rid] = [0 + 8 * (i % 2), 5 + 8 * (i // 2), -5]
        if self.task_id == "navigation":
            for rid in self.role_ids("speaker"):
                self.positions[rid] = [0, -10, -5]

    def _jitter_spawn_positions(self):
        """Keep layouts reproducible while avoiding one hard-coded fleet pose."""
        for position in self.positions.values():
            position[0] += self.rng.uniform(-4.0, 4.0)
            position[1] += self.rng.uniform(-4.0, 4.0)

    def _jitter_scene_objects(self):
        """Vary task landmarks while preserving formation geometry and semantics."""
        movable_kinds = {"landmark", "food", "forest", "treasure"}
        offsets = {}
        for obj in self.objects:
            if obj["kind"] not in movable_kinds:
                continue
            group = obj["id"].split("_")[0]
            offset = offsets.setdefault(
                group,
                (self.rng.uniform(-10.0, 10.0), self.rng.uniform(-10.0, 10.0)),
            )
            obj["position"][0] += offset[0]
            obj["position"][1] += offset[1]

    def _fit_layout_to_bounds(self):
        """Map a seeded layout into a user-selected rectangle before reset."""
        if not self.custom_bounds:
            return
        points = [position for position in self.positions.values()]
        points.extend(obj["position"] for obj in self.objects)
        if not points:
            return
        source_n = [point[0] for point in points]
        source_e = [point[1] for point in points]
        source_min_n, source_max_n = min(source_n), max(source_n)
        source_min_e, source_max_e = min(source_e), max(source_e)
        source_span_n = max(source_max_n - source_min_n, 1.0)
        source_span_e = max(source_max_e - source_min_e, 1.0)
        target_span_n = self.bounds["north_max"] - self.bounds["north_min"]
        target_span_e = self.bounds["east_max"] - self.bounds["east_min"]
        margin_n = min(4.0, max(1.0, target_span_n * 0.08))
        margin_e = min(4.0, max(1.0, target_span_e * 0.08))
        scale = min(
            max(0.2, target_span_n - 2.0 * margin_n) / source_span_n,
            max(0.2, target_span_e - 2.0 * margin_e) / source_span_e,
        )
        source_center_n = (source_min_n + source_max_n) / 2.0
        source_center_e = (source_min_e + source_max_e) / 2.0
        target_center_n = (self.bounds["north_min"] + self.bounds["north_max"]) / 2.0
        target_center_e = (self.bounds["east_min"] + self.bounds["east_max"]) / 2.0

        def remap(point):
            point[0] = target_center_n + (point[0] - source_center_n) * scale
            point[1] = target_center_e + (point[1] - source_center_e) * scale

        for point in self.positions.values():
            remap(point)
        for obj in self.objects:
            remap(obj["position"])

    def skills(self, rid):
        role = self.roles[rid]
        return list(ROLE_SKILLS[role]) + (["hold_position"] if role in STATIONARY else ["explore_local", "hold_position"])

    def agent_system_prompt(self, rid):
        """Build the isolated role context used by one body-bound local agent."""
        role = self.roles[rid]
        role_counts = ", ".join(
            f"{count} {name}" for name, count in sorted(Counter(self.roles.values()).items())
        )
        role_directives = {
            "searcher": "Cover visible landmarks while avoiding redundant coverage with peers.",
            "pursuer": "Pursue the assigned evader, maintain spacing, and coordinate through reachable peers.",
            "evader": "Avoid pursuers, keep moving inside the operating bounds, and use local evidence. Begin evasion when a visible pursuer is within 35 meters; do not wait for close contact.",
            "speaker": "Transmit the private goal through the task's communication action.",
            "listener": "Use reachable messages from the speaker to follow the communicated goal.",
            "sender": "Encode the private symbol with the private key and send the resulting message.",
            "receiver": "Decode messages using only the locally available private key.",
            "eavesdropper": "Infer the hidden message from the communication evidence available to you.",
            "formation_member": "Move toward your assigned formation slot and preserve separation.",
            "leader": "Pursue the local target and share useful target information with reachable peers.",
            "forager": "Seek visible food and use cover when it improves local task progress.",
            "collector": "Collect compatible treasures and deliver them to the matching local deposit.",
            "deposit_red": "Move toward a compatible red collector when local evidence supports it.",
            "deposit_blue": "Move toward a compatible blue collector when local evidence supports it.",
            "informed": "Approach the private goal while distinguishing it from public decoys.",
            "adversary": "Infer the hidden goal from local observations without global state.",
        }
        target_clause = ""
        if role == "pursuer" and self.task_id == "pursuit":
            evaders = self.role_ids("evader")
            if evaders:
                target_id = self.pursuit_target_by_robot.setdefault(
                    rid, evaders[self.role_ids("pursuer").index(rid) % len(evaders)]
                )
                target_clause = f" Your assigned local target is {target_id}."
        return (
            f"You are {rid}, a body-bound local agent in the {self.title} task. "
            f"Your role is {role}; the current role composition is {role_counts}. "
            f"Mission objective: {self.objective}. {role_directives.get(role, '')}"
            f"{target_clause} Use only your local observation and reachable peer messages. "
            "Select exactly one option from the supplied options. Do not control another UAV, "
            "invent a skill, or issue raw actuator commands. Return JSON with an integer "
            "option_index only."
        )

    def sync(self, snapshot):
        with self.lock:
            for rid in self.roles:
                if rid in snapshot:
                    self.positions[rid] = list(snapshot[rid]["position"])
                    self.velocities[rid] = list(snapshot[rid].get("velocity", [0, 0, 0]))

    def _covered(self, position):
        return any(o["kind"] == "forest" and distance(position, o["position"]) < o["radius"] for o in self.objects)

    def peers(self, rid):
        role = self.roles[rid]
        if self.task_id == "navigation":
            return self.role_ids("listener") if role == "speaker" else []
        if self.task_id == "private_communication":
            return self.role_ids("receiver") + self.role_ids("eavesdropper") if role == "sender" else []
        def team(r):
            if self.task_id in {"pursuit", "world_communication"}:
                return r in {"pursuer", "leader"}
            if self.task_id == "concealment":
                return r == "informed"
            return True
        return [other for other, r in self.roles.items() if other != rid and team(r) == team(role)
                and distance(self.positions[rid], self.positions[other]) <= self.communication_range]

    def observe(self, rid):
        with self.lock:
            role, own = self.roles[rid], self.positions[rid]
            radius = 100.0 if role == "leader" else self.sensor_range
            neighbors = []
            for other, pos in self.positions.items():
                if other == rid or distance(own, pos) > radius:
                    continue
                if self.task_id == "world_communication" and self._covered(pos) and not self._covered(own) and role != "leader":
                    continue
                neighbors.append({"id": other, "role": self.roles[other], "position": pos[:],
                                  "velocity": self.velocities[other][:], "cargo": self.cargo[other]})
            visible = [deepcopy(o) for o in self.objects if distance(own, o["position"]) <= radius or o["kind"] in {"slot", "center", "endpoint", "landmark"}]
            inbox = [deepcopy(m) for m in self.messages[-200:] if m["destination"] == rid and self.round - m["round"] <= 8]
            obs = {"robot_id": rid, "role": role, "task": self.task_id, "round": self.round,
                   "position": own[:], "velocity": self.velocities[rid][:], "neighbors": neighbors,
                   "objects": visible, "messages": inbox, "reachable_peers": self.peers(rid),
                   "cargo": self.cargo[rid], "skills": self.skills(rid), "bounds": dict(self.bounds)}
            if self.task_id == "pursuit" and role == "pursuer":
                pursuers = self.role_ids("pursuer")
                evaders = self.role_ids("evader")
                if evaders:
                    target_id = evaders[pursuers.index(rid) % len(evaders)]
                    self.pursuit_target_by_robot.setdefault(rid, target_id)
                    obs["pursuit_target_id"] = self.pursuit_target_by_robot[rid]
            if role in {"speaker", "informed"}:
                obs["private_goal"] = self.goal
            if role in {"sender", "receiver"}:
                obs["private_key"] = self.key
            if role == "sender":
                obs["private_symbol"] = self.symbol
            if self.task_id == "private_communication":
                obs["communication_protocol"] = {
                    "name": "xor_2bit_v1",
                    "symbol_domain": [0, 1, 2, 3],
                    "encoding": "ciphertext = plaintext XOR private_key (bitwise XOR, not addition)",
                    "decoding": "plaintext = ciphertext XOR private_key",
                    "decode_argument": "decode_message(symbol) submits the reconstructed plaintext; the executor does not decrypt its argument",
                    "delivery": "Use received ciphertext messages. Wait with hold_position until one is available.",
                }
            obs["options"] = self._options(obs)
            obs["agent_system_prompt"] = self.agent_system_prompt(rid)
            return obs

    @staticmethod
    def _options(obs):
        options = []
        def add(skill, target_id=None, **params):
            if skill in obs["skills"]:
                options.append({"skill": skill, "parameters": {**({"target_id": target_id} if target_id else {}), **params}})
        for obj in obs["objects"]:
            key, kind = obj["id"], obj["kind"]
            if kind == "landmark":
                add("cover_landmark", key)
                if key == obs.get("private_goal"):
                    add("approach_goal", key)
                elif "private_goal" in obs:
                    add("approach_decoy", key)
                add("infer_goal", key)
            if kind == "slot" and obj.get("owner") == obs["robot_id"]:
                add("take_formation_slot", key)
            if kind == "food":
                add("seek_food", key)
            if kind == "forest":
                add("seek_cover", key)
            if kind == "treasure" and not obs["cargo"] and not obj["carried_by"] and not obj["delivered"]:
                add("collect_treasure", key)
        for peer in obs["neighbors"]:
            key, role = peer["id"], peer["role"]
            if role in {"evader", "forager"}:
                add("pursue_target", key)
                add("signal_target", key)
            if role in {"pursuer", "leader"}:
                add("evade_peer", key)
            if role.startswith("deposit_") and obs["cargo"] and role == "deposit_" + obs["cargo"]["color"]:
                add("deliver_treasure", key)
            if role == "collector" and peer["cargo"] and obs["role"] == "deposit_" + peer["cargo"]["color"]:
                add("meet_collector", key)
        for msg in obs["messages"]:
            if msg["kind"] == "goal":
                add("follow_signal", msg["content"]["target_id"])
            if msg["kind"] == "target":
                add("follow_peer", msg["id"])
        if obs["role"] == "speaker":
            add("signal_goal", obs["private_goal"])
        if obs["role"] == "sender":
            add("encode_message", symbol=obs["private_symbol"] ^ obs["private_key"])
        if (obs["role"] in {"receiver", "eavesdropper"}
                and any(msg["kind"] == "ciphertext" for msg in obs["messages"])):
            for symbol in range(4):
                add("decode_message" if obs["role"] == "receiver" else "guess_message", symbol=symbol)
        add("return_in_bounds")
        add("explore_local")
        add("hold_position")
        return options

    def _send(self, rid, kind, content):
        for destination in self.peers(rid):
            message = {"id": f"msg_{len(self.messages)}", "source": rid, "destination": destination,
                       "kind": kind, "content": deepcopy(content), "round": self.round}
            self.messages.append(message)
            self.events.append(message)

    def apply(self, rid, skill, parameters, adapter):
        with self.lock:
            params = dict(parameters)
            obs = self.observe(rid)
            choice = {"skill": skill, "parameters": params}
            if choice not in obs["options"]:
                raise ValueError("Skill or target is outside this role's local task options")
            own = obs["position"]
            goal = None
            key = params.get("target_id")
            candidates = obs["objects"] + obs["neighbors"]
            target = next((o for o in candidates if o["id"] == key), None)
            if target:
                goal = target["position"][:]
            if skill == "signal_goal":
                self._send(rid, "goal", {"target_id": self.goal})
            elif skill == "encode_message":
                self._send(rid, "ciphertext", {"symbol": params["symbol"]})
            elif skill in {"decode_message", "guess_message"}:
                self.guesses[rid] = params["symbol"]
            elif skill == "signal_target":
                self._send(rid, "target", {"target_id": key, "position": goal})
            elif skill == "follow_peer":
                goal = next(m["content"]["position"] for m in obs["messages"] if m["id"] == key)
            elif skill == "evade_peer":
                goal = [own[0] + own[0] - goal[0], own[1] + own[1] - goal[1], own[2]]
            elif skill == "return_in_bounds":
                goal = [0, 0, -5]
            elif skill == "explore_local":
                angle = self.round / 18 + int(rid.split("_")[-1]) * 1.7
                goal = [35 * math.cos(angle), 35 * math.sin(angle), -5]
            if self.task_id == "pursuit" and obs["role"] == "evader" and skill == "evade_peer":
                # Persist the selected threat, not a waypoint that the body can
                # reach while its next skill-selection request is pending.
                self.evasion_states[rid] = {"target_id": key, "direction": [
                    goal[0] - own[0], goal[1] - own[1],
                ]}
            else:
                self.evasion_states.pop(rid, None)
            if skill == "collect_treasure" and distance(own, goal) <= 3:
                obj = next(o for o in self.objects if o["id"] == key)
                obj["carried_by"] = rid
                self.cargo[rid] = {"id": key, "color": obj["color"]}
            if skill == "deliver_treasure" and distance(own, goal) <= 3:
                obj = next(o for o in self.objects if o["id"] == self.cargo[rid]["id"])
                obj["delivered"] = True
                self.cargo[rid] = None
                self.delivered += 1
            if skill == "seek_food" and distance(own, goal) <= 3:
                next(o for o in self.objects if o["id"] == key)["visits"] += 1
            if obs["role"] in STATIONARY or skill in {"hold_position", "signal_target"}:
                velocity = [0.0, 0.0, 0.0]
                self.motion_targets.pop(rid, None)
            else:
                velocity = self._velocity(obs, goal)
                self.motion_targets[rid] = deepcopy(goal)
            result = adapter.set_velocity_ned_for(rid, *velocity)
            if not result.success:
                raise RuntimeError(result.message)
            if skill == "pursue_target":
                self._send(rid, "target", {"target_id": key, "position": target["position"]})
            elif skill not in {"hold_position", "return_in_bounds"} and self.task_id != "private_communication":
                # Expose the local agent's short state/intent exchange without
                # leaking global state or turning the trace into a broadcast.
                self._send(rid, "intent", {
                    "skill": skill,
                    "position": own[:],
                    "target_id": key,
                })
            return {"robot_id": rid, "role": obs["role"], "skill": skill,
                    "velocity_ned": velocity, "persistent": True, "native_mpe": False}

    def refresh_motion(self, adapter):
        """Refresh the selected motion skill while its next decision is pending."""
        self.sync(adapter.get_robot_snapshot())
        with self.lock:
            for rid, goal in self.motion_targets.items():
                adapter.set_velocity_ned_for(rid, *self._velocity(self.observe(rid), goal))

    def _velocity(self, obs, goal):
        if goal is None:
            return [0.0, 0.0, 0.0]
        own = obs["position"]
        pursuit_target = None
        if obs.get("task") == "pursuit" and obs.get("role") == "pursuer":
            evaders = [item for item in obs.get("neighbors", []) if item.get("role") == "evader"]
            if evaders:
                # Keep one local target for this pursuer. Re-selecting the
                # closest evader at every physics tick makes a multi-evader
                # pursuit switch targets and visibly oscillate.
                target_id = self.pursuit_target_by_robot.get(obs["robot_id"])
                if target_id is None:
                    target_id = obs.get("pursuit_target_id")
                    if target_id:
                        self.pursuit_target_by_robot[obs["robot_id"]] = target_id
                pursuit_target = next((item for item in evaders if item.get("id") == target_id), None)
                pursuit_target = pursuit_target or min(evaders, key=lambda item: distance(item["position"], goal))
                # Keep the slot count at the configured pursuer count while
                # peers are still reachable. A changing visible-neighbor set
                # otherwise changes the angular slot and reverses the command.
                pursuer_order = self.role_ids("pursuer")
                slot_index = pursuer_order.index(obs["robot_id"])
                slot_count = max(1, len(pursuer_order))
                target_position = pursuit_target["position"]
                target_velocity = pursuit_target.get("velocity", [0.0, 0.0, 0.0])
                lead = min(0.45, max(0.18, distance(own, target_position) / 28.0))
                ring_radius = min(4.8, max(3.8, 1.8 + slot_count * 0.65))
                angle = 2.0 * math.pi * slot_index / slot_count
                goal = [
                    target_position[0] + target_velocity[0] * lead + ring_radius * math.cos(angle),
                    target_position[1] + target_velocity[1] * lead + ring_radius * math.sin(angle),
                    target_position[2],
                ]
        vec = [(goal[i] - own[i]) * 1.2 - obs["velocity"][i] * 0.35 for i in range(2)]
        if distance(own, goal) < 0.8:
            vec = [0.0, 0.0]
        evasion = self.evasion_states.get(obs["robot_id"])
        if evasion is not None:
            threat = next((other for other in obs["neighbors"]
                           if other["id"] == evasion["target_id"]), None)
            if threat is not None:
                direction = [own[i] - threat["position"][i] for i in range(2)]
                if math.hypot(*direction) > 1e-9:
                    evasion["direction"] = direction
            # Continue along the last locally observed escape bearing if the
            # selected threat leaves sensing range. Never read hidden positions.
            direction = evasion["direction"]
            norm = math.hypot(*direction)
            if norm <= 1e-9:
                direction, norm = [1.0, 0.0], 1.0
            vec = [component / norm * 15.0 for component in direction]
        # Only locally visible teammates contribute to short-range separation.
        for other in obs["neighbors"]:
            if other["role"] != obs["role"] or obs["role"] in {"pursuer", "leader", "forager"}:
                continue
            d = distance(own, other["position"])
            if 0.01 < d < 4:
                for i in range(2):
                    vec[i] += 2 * (own[i] - other["position"][i]) / d * (4-d)
        if pursuit_target is not None:
            for other in obs.get("neighbors", []):
                if other.get("role") != "pursuer":
                    continue
                d = distance(own, other["position"])
                if 0.01 < d < 3.8:
                    strength = (3.8 - d) / 3.8 * 5.0
                    vec[0] += (own[0] - other["position"][0]) / d * strength
                    vec[1] += (own[1] - other["position"][1]) / d * strength
        speed = 15.0 if obs["role"] in {"evader", "forager"} else 11.0
        if pursuit_target is not None:
            speed = min(speed, max(2.0, distance(own, goal) * 1.45))

        # Apply an anticipatory inward correction before the adapter's hard
        # boundary shield clips a command. This prevents edge oscillation and
        # leaves room to brake inside a user-selected mission rectangle.
        bounds = obs.get("bounds") or {}
        if all(key in bounds for key in ("north_min", "north_max", "east_min", "east_max")):
            spans = (bounds["north_max"] - bounds["north_min"], bounds["east_max"] - bounds["east_min"])
            for axis, lower_key, upper_key, span in (
                (0, "north_min", "north_max", spans[0]),
                (1, "east_min", "east_max", spans[1]),
            ):
                braking_margin = min(max(2.0, speed * 0.35), max(1.0, span * 0.30))
                lower_distance = own[axis] - bounds[lower_key]
                upper_distance = bounds[upper_key] - own[axis]
                if lower_distance < braking_margin:
                    vec[axis] = max(vec[axis], (braking_margin - lower_distance) * 2.5)
                if upper_distance < braking_margin:
                    vec[axis] = min(vec[axis], -(braking_margin - upper_distance) * 2.5)

        if obs["role"] == "evader":
            for other in obs["neighbors"]:
                if other["role"] not in {"pursuer", "leader"}:
                    continue
                d = distance(own, other["position"])
                if 0.01 < d < 8.0:
                    strength = (8.0 - d) / 8.0 * speed
                    vec[0] += (own[0] - other["position"][0]) / d * strength
                    vec[1] += (own[1] - other["position"][1]) / d * strength
        pursuit_motion = obs.get("task") == "pursuit" and obs.get("role") in {"pursuer", "evader"}
        if pursuit_motion and evasion is None:
            # Low-pass the command before handing it to the point-mass
            # dynamics. The target slot and local observations update more
            # often than the body can change velocity, so abrupt reversals
            # should be attenuated rather than sent to the actuator.
            norm = math.hypot(*vec)
            if norm > 1e-9:
                desired = [vec[0] / norm * speed, vec[1] / norm * speed]
                previous = self.motion_commands.get(obs["robot_id"])
                if previous:
                    dot = previous[0] * desired[0] + previous[1] * desired[1]
                    alpha = 0.08 if dot < 0.0 else 0.18
                    desired = [
                        previous[0] * (1.0 - alpha) + desired[0] * alpha,
                        previous[1] * (1.0 - alpha) + desired[1] * alpha,
                    ]
                vec = desired
                self.motion_commands[obs["robot_id"]] = [desired[0], desired[1]]
        norm = math.hypot(*vec)
        if norm > speed:
            vec = [v * speed / norm for v in vec]
        for i in range(2):
            if abs(own[i]) > 65 and own[i] * vec[i] > 0:
                vec[i] = -math.copysign(speed / 2, own[i])
        return vec + [0.0]

    def evaluate(self):
        with self.lock:
            self.round += 1
            metrics = {}
            complete = False
            if self.task_id == "coverage":
                metrics["covered_landmarks"] = sum(min(distance(p, o["position"]) for p in self.positions.values()) < 3 for o in self.objects)
                complete = metrics["covered_landmarks"] == len(self.objects)
            elif self.task_id in {"circle", "line"}:
                errors = [distance(self.positions[o["owner"]], o["position"]) for o in self.objects if o["kind"] == "slot"]
                metrics["mean_slot_error_m"] = sum(errors) / len(errors)
                complete = max(errors) < 3
            elif self.task_id == "navigation":
                goal = next(o for o in self.objects if o["id"] == self.goal)
                listener_distances = [distance(self.positions[rid], goal["position"]) for rid in self.role_ids("listener")]
                metrics["listener_goal_distance_m"] = max(listener_distances)
                metrics["listeners_at_goal"] = sum(d < 3 for d in listener_distances)
                complete = metrics["listener_goal_distance_m"] < 3
            elif self.task_id == "private_communication":
                receiver_ids = self.role_ids("receiver")
                eavesdropper_ids = self.role_ids("eavesdropper")
                metrics = {
                    "receiver_correct": bool(receiver_ids) and all(self.guesses.get(rid) == self.symbol for rid in receiver_ids),
                    "eavesdropper_correct": bool(eavesdropper_ids) and all(self.guesses.get(rid) == self.symbol for rid in eavesdropper_ids),
                    "receivers_correct": sum(self.guesses.get(rid) == self.symbol for rid in receiver_ids),
                    "eavesdroppers_correct": sum(self.guesses.get(rid) == self.symbol for rid in eavesdropper_ids),
                }
                complete = self.round >= 3
            elif self.task_id == "pursuit":
                pursuer_ids, evader_ids = self.role_ids("pursuer"), self.role_ids("evader")
                evader_distances = [min(distance(self.positions[p], self.positions[e]) for p in pursuer_ids) for e in evader_ids]
                capture_counts = [sum(distance(self.positions[p], self.positions[e]) < 6 for p in pursuer_ids) for e in evader_ids]
                metrics = {
                    "evaders_in_capture_range": sum(count > 0 for count in capture_counts),
                    "captured_evaders": sum(count >= 2 for count in capture_counts),
                    "evader_count": len(evader_ids),
                    "closest_pursuer_m": min(evader_distances),
                }
                complete = metrics["captured_evaders"] == len(evader_ids)
            elif self.task_id == "collection":
                metrics["deliveries"] = self.delivered
                complete = self.delivered == len(self.objects)
            elif self.task_id == "world_communication":
                metrics["food_visits"] = sum(o.get("visits", 0) for o in self.objects)
                forager_ids = self.role_ids("forager")
                coordination_ids = self.role_ids("pursuer") + self.role_ids("leader")
                metrics["foragers_in_cover"] = sum(self._covered(self.positions[rid]) for rid in forager_ids)
                metrics["pursuit_contacts"] = sum(
                    distance(self.positions[a], self.positions[b]) < 3
                    for a in coordination_ids for b in forager_ids
                )
            elif self.task_id == "concealment":
                goal = next(o for o in self.objects if o["id"] == self.goal)
                informed_distances = [distance(self.positions[rid], goal["position"]) for rid in self.role_ids("informed")]
                adversary_distances = [distance(self.positions[rid], goal["position"]) for rid in self.role_ids("adversary")]
                metrics["informed_goal_distance_m"] = min(informed_distances)
                metrics["adversary_goal_distance_m"] = min(adversary_distances)
            positions = list(self.positions.values())
            metrics["near_collisions"] = sum(distance(a, b) < 2 for i, a in enumerate(positions) for b in positions[i+1:])
            self.stable_rounds = self.stable_rounds + 1 if complete else 0
            if self.stable_rounds >= (1 if self.task_id in {"pursuit", "private_communication", "collection"} else 3):
                self.status = "complete"
            elif self.round >= self.max_rounds:
                self.status = "horizon"
            self.metrics = metrics
            return deepcopy(metrics)

    def snapshot(self):
        with self.lock:
            return {"task_id": self.task_id, "mission_id": getattr(self, "mission_id", ""), "title": self.title, "objective": self.objective,
                    "roles": dict(self.roles), "role_counts": dict(Counter(self.roles.values())),
                    "requested_role_counts": dict(self.requested_role_counts),
                    "requested_role_assignments": dict(self.requested_role_assignments),
                    "scenario_id": self.task_id, "parser_source": self.parser_source,
                    "requested_fleet_size": self.requested_fleet_size,
                    "active_skills": self.active_skills[:],
                    "role_skills": {rid: self.skills(rid) for rid in self.roles},
                    "objects": deepcopy(self.objects), "round": self.round, "status": self.status,
                    "metrics": deepcopy(self.metrics), "seed": self.seed, "native_mpe": False,
                    "substrate": "mock_point_mass",
                    "reward_source": getattr(getattr(self, "reward_model", None), "source", "not_attached"),
                    "bounds": dict(self.bounds)}


def local_skill_choice(obs):
    """Deterministic engineering baseline; receives no simulator-wide task state."""
    options = obs["options"]
    def first(skill):
        return next((o for o in options if o["skill"] == skill), None)
    def nearest(skill):
        candidates = [o for o in options if o["skill"] == skill]
        positions = {x["id"]: x["position"] for x in obs["objects"] + obs["neighbors"]}
        return min(candidates, key=lambda o: distance(obs["position"], positions[o["parameters"]["target_id"]]), default=None)
    role = obs["role"]
    choice = None
    if role == "searcher":
        landmarks = [o for o in obs["objects"] if o["kind"] == "landmark"]
        # Public landmark identities break symmetric choices without fleet-state access.
        target = landmarks[(int(obs["robot_id"].split("_")[-1])-1) % len(landmarks)]["id"]
        choice = next(o for o in options if o["skill"] == "cover_landmark" and o["parameters"]["target_id"] == target)
    elif role in {"pursuer", "leader"}:
        target_id = obs.get("pursuit_target_id")
        if target_id:
            choice = next(
                (
                    option for option in options
                    if option["skill"] == "pursue_target"
                    and option["parameters"].get("target_id") == target_id
                ),
                None,
            )
        choice = choice or nearest("pursue_target") or first("follow_peer")
    elif role in {"evader", "forager"}:
        threats = [x for x in obs["neighbors"] if x["role"] in {"pursuer", "leader"}]
        evasion_distance = 35.0 if role == "evader" and obs["task"] == "pursuit" else 18.0
        if threats and min(distance(obs["position"], x["position"]) for x in threats) < evasion_distance:
            choice = nearest("evade_peer")
        if role == "forager":
            choice = choice or (nearest("seek_cover") if obs["round"] % 30 > 20 else nearest("seek_food"))
    elif role == "speaker":
        choice = first("signal_goal")
    elif role == "listener":
        choice = first("follow_signal") or first("hold_position")
    elif role == "sender":
        choice = first("encode_message")
    elif role in {"receiver", "eavesdropper"}:
        cipher = next((m["content"]["symbol"] for m in reversed(obs["messages"]) if m["kind"] == "ciphertext"), None)
        if cipher is not None:
            symbol = cipher ^ obs["private_key"] if role == "receiver" else cipher
            choice = next(o for o in options if o["parameters"].get("symbol") == symbol)
    elif role == "formation_member":
        choice = first("take_formation_slot")
    elif role == "collector":
        choice = nearest("deliver_treasure") if obs["cargo"] else nearest("collect_treasure")
    elif role.startswith("deposit_"):
        choice = nearest("meet_collector") or first("hold_position")
    elif role == "informed":
        choice = first("approach_goal")
    elif role == "adversary":
        informed = [x for x in obs["neighbors"] if x["role"] == "informed"]
        if informed:
            landmarks = [x for x in obs["objects"] if x["kind"] == "landmark"]
            target = min(landmarks, key=lambda x: min(distance(x["position"], p["position"]) for p in informed))
            choice = next(o for o in options if o["skill"] == "infer_goal" and o["parameters"]["target_id"] == target["id"])
    return deepcopy(choice or first("explore_local") or first("hold_position"))
