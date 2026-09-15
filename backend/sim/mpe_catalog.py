"""Official MPE/MPE2 scenario catalog and optional runtime factory.

The catalog is intentionally explicit: MPE2 has a stable, documented suite,
while arbitrary MPE-derived research repositories do not share one API.
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any


MPE2_SCENARIOS: tuple[dict[str, Any], ...] = (
    {"id": "simple", "family": "debug", "cooperative": True, "competitive": False, "default_agents": 1, "roles": ["agent"], "description": "Single agent navigates to one landmark for debugging.", "versions": ["v3"]},
    {"id": "simple_adversary", "family": "deception", "cooperative": False, "competitive": True, "default_agents": 3, "roles": ["good_agent", "adversary"], "description": "Good agents split across landmarks to deceive an adversary.", "versions": ["v3"]},
    {"id": "simple_crypto", "family": "communication", "cooperative": False, "competitive": True, "default_agents": 3, "roles": ["sender", "receiver", "eavesdropper"], "description": "Two allied agents communicate privately in the presence of an eavesdropper.", "versions": ["v3"]},
    {"id": "simple_formation", "family": "formation", "cooperative": True, "competitive": False, "default_agents": 4, "roles": ["formation_member"], "description": "Agents arrange themselves on a circle around a central landmark.", "versions": ["v1"]},
    {"id": "simple_line", "family": "formation", "cooperative": True, "competitive": False, "default_agents": 4, "roles": ["formation_member"], "description": "Agents arrange themselves along a line between two landmarks.", "versions": ["v1"]},
    {"id": "simple_push", "family": "interaction", "cooperative": False, "competitive": True, "default_agents": 2, "roles": ["pusher", "adversary"], "description": "An agent reaches a landmark while an adversary pushes it away.", "versions": ["v3"]},
    {"id": "simple_reference", "family": "communication", "cooperative": True, "competitive": False, "default_agents": 2, "roles": ["speaker", "listener"], "description": "Agents communicate target landmark references and navigate cooperatively.", "versions": ["v3"]},
    {"id": "simple_speaker_listener", "family": "communication", "cooperative": True, "competitive": False, "default_agents": 2, "roles": ["speaker", "listener"], "description": "A stationary speaker communicates the listener target.", "versions": ["v4", "v3"]},
    {"id": "simple_spread", "family": "coverage", "cooperative": True, "competitive": False, "default_agents": 3, "roles": ["searcher"], "description": "Agents cover landmarks while avoiding collisions.", "versions": ["v3"]},
    {"id": "simple_tag", "family": "pursuit", "cooperative": False, "competitive": True, "default_agents": 4, "roles": ["pursuer", "evader"], "description": "Faster good agents evade slower adversaries around obstacles.", "versions": ["v3"]},
    {"id": "simple_world_comm", "family": "partial_observation", "cooperative": False, "competitive": True, "default_agents": 6, "roles": ["leader_adversary", "adversary", "good_agent"], "description": "A communicating leader adversary coordinates a partially observed pursuit with food and forests.", "versions": ["v3"]},
    {"id": "collect_treasure", "family": "transport", "cooperative": True, "competitive": False, "default_agents": 8, "roles": ["collector", "depositor"], "description": "Collectors retrieve typed treasures and deliver them to matching deposit agents.", "versions": ["v1"]},
)

PAPER_SCENARIOS = (
    "simple_spread", "simple_tag", "simple_speaker_listener", "simple_crypto",
    "simple_formation", "simple_line", "simple_world_comm", "collect_treasure",
    "simple_adversary",
)


def _scenario(scenario_id: str) -> dict[str, Any]:
    for item in MPE2_SCENARIOS:
        if item["id"] == str(scenario_id):
            return dict(item)
    raise KeyError(f"Unknown MPE2 scenario: {scenario_id}")


def _resolve_entrypoint(item: dict[str, Any]):
    if importlib.util.find_spec("mpe2") is None:
        return None, None
    package = importlib.import_module("mpe2")
    for version in item["versions"]:
        name = f"{item['id']}_{version}"
        entrypoint = getattr(package, name, None)
        if entrypoint is None:
            try:
                entrypoint = importlib.import_module(f"mpe2.{name}")
            except ImportError:
                entrypoint = None
        if entrypoint is not None:
            return name, entrypoint
    return None, None


def list_mpe2_environments() -> list[dict[str, Any]]:
    """List every official MPE2 scenario and its local availability."""
    result = []
    for item in MPE2_SCENARIOS:
        entrypoint, _ = _resolve_entrypoint(item)
        row = dict(item)
        row["source"] = "Farama MPE2"
        row["entrypoint"] = entrypoint
        row["installed"] = entrypoint is not None
        result.append(row)
    return result


def create_mpe2_environment(scenario_id: str, **kwargs):
    """Instantiate one MPE2 environment without coupling AeroWeaver to its API."""
    item = _scenario(scenario_id)
    entrypoint, factory = _resolve_entrypoint(item)
    if factory is None:
        raise RuntimeError("MPE2 is not installed. Install requirements/mpe2.txt.")
    make = getattr(factory, "parallel_env", None) or getattr(factory, "env", None)
    if make is None:
        raise RuntimeError(f"MPE2 entrypoint {entrypoint} has no env factory")
    from sim.mpe_resources import create_managed_environment
    return create_managed_environment(make, **kwargs)


def role_for_instance(scenario_id: str, agent_id: str, index: int = 0) -> str:
    """Map a concrete MPE2 instance name to a semantic role."""
    scenario = str(scenario_id)
    name = str(agent_id or "").lower()
    prefixes = {
        "simple_adversary": (("adversary", "adversary"), ("agent", "good_agent")),
        "simple_crypto": (("eve", "eavesdropper"), ("alice", "sender"), ("bob", "receiver")),
        "simple_push": (("adversary", "adversary"), ("agent", "pusher")),
        "simple_reference": (("agent_0", "speaker"), ("agent_1", "listener")),
        "simple_speaker_listener": (("speaker", "speaker"), ("listener", "listener")),
        "simple_tag": (("adversary", "pursuer"), ("agent", "evader")),
        "simple_world_comm": (("leadadversary", "leader_adversary"), ("adversary", "adversary"), ("agent", "good_agent")),
        "collect_treasure": (("collector", "collector"), ("deposit", "depositor")),
    }
    for prefix, role in prefixes.get(scenario, ()):
        if name.startswith(prefix):
            return role
    item = _scenario(scenario)
    roles = list(item["roles"])
    return roles[min(max(int(index), 0), len(roles) - 1)]


def probe_mpe2_environment(scenario_id: str, seed: int = 0, **kwargs) -> dict[str, Any]:
    """Reset, inspect, and close a scenario to verify that it is executable."""
    env = create_mpe2_environment(scenario_id, **kwargs)
    try:
        reset_result = env.reset(seed=seed)
        agents = list(getattr(env, "possible_agents", None) or getattr(env, "agents", None) or [])
        return {
            "scenario_id": scenario_id,
            "agents": agents,
            "agent_count": len(agents),
            "role_by_agent": {
                agent: role_for_instance(scenario_id, agent, index)
                for index, agent in enumerate(agents)
            },
            "reset_result_type": type(reset_result).__name__,
        }
    finally:
        close = getattr(env, "close", None)
        if callable(close):
            close()


def role_mapping_for_scenario(scenario_id: str) -> dict[str, str]:
    """Return native default instance bindings, not index-to-role guesses."""
    item = _scenario(scenario_id)
    names = {
        "simple_tag": ["adversary_0", "adversary_1", "adversary_2", "agent_0"],
        "simple_adversary": ["adversary_0", "agent_0", "agent_1"],
        "simple_crypto": ["eve_0", "bob_0", "alice_0"],
        "simple_speaker_listener": ["speaker_0", "listener_0"],
        "simple_push": ["adversary_0", "agent_0"],
        "simple_world_comm": ["leadadversary_0", "adversary_0", "adversary_1", "adversary_2", "agent_0", "agent_1"],
        "collect_treasure": [f"collector_{i}" for i in range(6)] + ["deposit_0", "deposit_1"],
    }.get(scenario_id, [f"agent_{i}" for i in range(item["default_agents"])])
    if scenario_id == "simple_reference":
        return {name: "speaker_listener" for name in names}
    return {name: role_for_instance(scenario_id, name, i) for i, name in enumerate(names)}
