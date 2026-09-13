"""Role semantics shared by AeroWeaver agents and MPE2 scenario adapters."""

from __future__ import annotations

import re
from typing import Any


ROLE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"role": "agent", "label": "Generic agent", "family": "neutral", "description": "A movable agent without a specialized assignment."},
    {"role": "commander", "label": "Commander", "family": "coordination", "description": "Assigns mission objectives and termination conditions; has no physical body."},
    {"role": "coordinator", "label": "Coordinator", "family": "coordination", "description": "Maintains team-level coordination and shared state."},
    {"role": "searcher", "label": "Searcher", "family": "mission", "description": "Covers a region or distributes across targets and landmarks."},
    {"role": "scout", "label": "Scout", "family": "mission", "description": "Explores and reports local observations before the team commits."},
    {"role": "tracker", "label": "Tracker", "family": "mission", "description": "Maintains target state and follows a moving object."},
    {"role": "inspector", "label": "Inspector", "family": "mission", "description": "Inspects a waypoint, object, or region and returns evidence."},
    {"role": "patroller", "label": "Patroller", "family": "mission", "description": "Maintains coverage along a route or perimeter."},
    {"role": "pursuer", "label": "Pursuer", "family": "adversarial", "description": "Coordinates to intercept or capture an evader."},
    {"role": "evader", "label": "Evader", "family": "adversarial", "description": "Avoids pursuit while remaining inside mission constraints."},
    {"role": "good_agent", "label": "Good Agent", "family": "adversarial", "description": "Cooperates with the allied team in an MPE adversarial scenario."},
    {"role": "adversary", "label": "Adversary", "family": "adversarial", "description": "Competes against the good-agent team."},
    {"role": "leader_adversary", "label": "Leader Adversary", "family": "adversarial", "description": "Has privileged visibility or communication for adversarial coordination."},
    {"role": "relay", "label": "Relay", "family": "communication", "description": "Maintains communication coverage or forwards team state."},
    {"role": "speaker", "label": "Speaker", "family": "communication", "description": "Knows or encodes task information for another agent."},
    {"role": "listener", "label": "Listener", "family": "communication", "description": "Decodes teammate information and executes the implied goal."},
    {"role": "sender", "label": "Sender", "family": "communication", "description": "Sends a private or task-specific message."},
    {"role": "receiver", "label": "Receiver", "family": "communication", "description": "Reconstructs or acts on a teammate message."},
    {"role": "eavesdropper", "label": "Eavesdropper", "family": "adversarial", "description": "Attempts to infer a private communication."},
    {"role": "pusher", "label": "Pusher", "family": "interaction", "description": "Moves toward or manipulates a shared landmark or object."},
    {"role": "collector", "label": "Collector", "family": "interaction", "description": "Collects a target object and transports it."},
    {"role": "depositor", "label": "Depositor", "family": "interaction", "description": "Receives or positions near objects for delivery."},
    {"role": "formation_member", "label": "Formation Member", "family": "formation", "description": "Occupies a role-free slot in a team formation."},
    {"role": "navigator", "label": "Navigator", "family": "navigation", "description": "Moves to a target landmark or assigned geometric position."},
)

ROLE_ALIASES = {
    "uav": "agent",
    "robot": "agent",
    "good": "good_agent",
    "prey": "evader",
    "predator": "pursuer",
    "leader": "leader_adversary",
    "formation": "formation_member",
    "member": "formation_member",
    "collector_agent": "collector",
    "deposit": "depositor",
}

_VALID_ROLES = {item["role"] for item in ROLE_DEFINITIONS}

_SKILL_ROLE_RULES = (
    ("leader_adversary", ("leader_adversary", "lead_adversary")),
    ("eavesdropper", ("eavesdrop", "crypto_adversary", "eve")),
    ("depositor", ("deposit", "depositor", "deliver")),
    ("collector", ("collect", "collector", "pickup", "pick_up")),
    ("formation_member", ("formation", "simple_line", "simple_formation")),
    ("pursuer", ("pursuit", "pursuer", "intercept", "encircle", "capture", "chase", "tag")),
    ("evader", ("evader", "escape", "avoid_pursuit", "hide")),
    ("tracker", ("track", "follow", "target_lock")),
    ("searcher", ("search", "spread", "coverage", "explore")),
    ("patroller", ("patrol", "perimeter", "orbit")),
    ("inspector", ("inspect", "waypoint", "survey")),
    ("relay", ("relay", "communication", "comm_", "link")),
    ("speaker", ("speaker", "speak", "encode", "message")),
    ("listener", ("listener", "listen", "decode", "receive")),
    ("pusher", ("push", "move_object")),
    ("navigator", ("goto", "go_to", "navigate", "move")),
)


def normalize_role(value: Any, default: str = "agent") -> str:
    """Normalize a role label without ever deriving it from a UAV number."""
    candidate = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    candidate = ROLE_ALIASES.get(candidate, candidate)
    return candidate if candidate in _VALID_ROLES else default


def infer_role(*, skill: Any = "", task: Any = "", metadata: dict[str, Any] | None = None, default: str = "agent") -> str:
    """Infer a semantic role from explicit metadata, skill, and task text."""
    metadata = dict(metadata or {})
    for key in ("role", "agent_role", "assignment_role", "task_role"):
        if metadata.get(key):
            return normalize_role(metadata[key], default=default)
    text = " ".join(str(value or "").lower() for value in (skill, task))
    tokens = set(re.findall(r"[a-z0-9_]+", text))
    for role, terms in _SKILL_ROLE_RULES:
        for term in terms:
            if len(term) <= 3:
                matched = term in tokens or any(token.startswith(f"{term}_") or token.endswith(f"_{term}") for token in tokens)
            else:
                matched = any(term in token for token in tokens)
            if matched:
                return role
    return normalize_role(default, default="agent")


def role_catalog() -> list[dict[str, Any]]:
    """Return a JSON-safe copy for APIs and the console."""
    return [dict(item) for item in ROLE_DEFINITIONS]
