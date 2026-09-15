"""Observation-only engineering selectors. These are NOT LLM baselines."""

import math
import numpy as np


def local_candidates(scenario, spec, observation, context):
    """Build parameterized skills using only the participant's native observation.

    Context contains immutable scenario sizes and public native role identities,
    never world coordinates, private goals, keys, rewards, or teammate observations.
    """
    obs = np.asarray(observation, dtype=float)
    candidates = []
    velocity = obs[:2] if scenario not in {"simple_crypto", "simple_adversary"} else np.zeros(2)
    if scenario == "collect_treasure":
        velocity = obs[2:4]

    def add(name, delta=None, score=0.0, symbol=None):
        params = {}
        if delta is not None:
            params.update(target_delta=np.asarray(delta).tolist(), velocity=velocity.tolist())
        if symbol is not None:
            params["symbol"] = int(symbol)
        candidates.append({"agent_id": spec.agent_id, "skill": name, "parameters": params, "base_score": float(score)})

    n_landmarks = context["landmark_count"]
    if scenario == "simple_spread":
        landmarks = obs[4:4 + 2 * n_landmarks].reshape(-1, 2)
        for i, target in enumerate(landmarks):
            add("cover_landmark", target, 1.0 if i == spec.index % len(landmarks) else -float(np.linalg.norm(target)))
        others = obs[4 + 2 * n_landmarks:4 + 2 * n_landmarks + 2 * (context["agent_count"] - 1)].reshape(-1, 2)
        for target in others:
            add("avoid_collision", target, 1.5 if np.linalg.norm(target) < 0.18 else -1)
    elif scenario in {"simple_formation", "simple_line"}:
        if scenario == "simple_formation":
            angle = 2 * math.pi * spec.index / context["agent_count"]
            delta = obs[4:6] + 0.5 * np.array([math.cos(angle), math.sin(angle)])
            add("take_circle_slot", delta, 1)
        else:
            fraction = spec.index / max(1, context["agent_count"] - 1)
            add("take_line_slot", (1 - fraction) * obs[4:6] + fraction * obs[6:8], 1)
    elif scenario == "simple_speaker_listener":
        if spec.role == "speaker":
            for symbol in range(spec.symbols):
                add("signal_goal", symbol=symbol, score=1 if symbol == int(np.argmax(obs)) else 0)
        else:
            goal = int(np.argmax(obs[-3:]))
            for i, target in enumerate(obs[2:8].reshape(-1, 2)):
                add("navigate_to_landmark", target, 1 if i == goal else 0)
    elif scenario == "simple_crypto":
        symbol = int(np.argmax(obs[:4]))
        if spec.role in {"sender", "receiver"}:
            symbol ^= int(np.argmax(obs[4:8]))
        name = {"sender": "encode_message", "receiver": "decode_message", "eavesdropper": "guess_message"}[spec.role]
        for value in range(spec.symbols):
            add(name, symbol=value, score=1 if value == symbol else 0)
    elif scenario == "simple_adversary":
        offset = 2 if spec.role == "good_agent" else 0
        landmarks = obs[offset:offset + n_landmarks * 2].reshape(-1, 2)
        if spec.role == "good_agent":
            add("approach_goal", obs[:2], 1)
            for target in landmarks:
                add("approach_decoy", target, 0)
        else:
            others = obs[n_landmarks * 2:].reshape(-1, 2)
            for target in landmarks:
                distance = min(float(np.linalg.norm(target - other)) for other in others)
                add("approach_inferred_goal", target, -distance)
    elif scenario in {"simple_tag", "simple_world_comm"}:
        others = [a for a in context["agents"] if a["agent_id"] != spec.agent_id]
        offset = 4 + 2 * n_landmarks
        positions = obs[offset:offset + 2 * len(others)].reshape(-1, 2)
        for other, target in zip(others, positions):
            if other["team"] == spec.team:
                continue
            if scenario == "simple_world_comm" and np.linalg.norm(target) == 0:
                continue  # Native forest masking; do not recover hidden world state.
            if spec.team == "pursuers":
                score = 1 - float(np.linalg.norm(target))
                if spec.role == "leader_adversary":
                    direction = int(np.argmax(np.abs(target))) * 2 + int(target[int(np.argmax(np.abs(target)))] > 0)
                    add("pursue_and_signal", target, score, direction)
                else:
                    add("pursue_target", target, score)
            else:
                add("evade_pursuer", target, 1.5 if np.linalg.norm(target) < 0.6 else -0.5)
        if scenario == "simple_tag" and spec.role == "evader":
            add("return_in_bounds", -obs[2:4], 2 if np.max(np.abs(obs[2:4])) > 0.85 else -1)
        if scenario == "simple_world_comm":
            if spec.role == "good_agent":
                for index in context["food_indices"]:
                    target = obs[4 + index * 2:6 + index * 2]
                    add("seek_food", target, 1 - float(np.linalg.norm(target)))
                for index in context["forest_indices"]:
                    add("seek_cover", obs[4 + index * 2:6 + index * 2], -0.2)
            elif spec.role == "adversary":
                direction = int(np.argmax(obs[-4:]))
                vectors = [(-1, 0), (1, 0), (0, -1), (0, 1)]
                add("follow_signal", vectors[direction], -0.1 if np.any(obs[-4:]) else -2)
    elif scenario == "collect_treasure":
        n = context["treasure_types"]
        holding = obs[4:4 + n] if spec.role == "collector" else np.zeros(n)
        offset = 4 + (n if spec.role == "collector" else 0)
        stride = 4 + 2 * n
        others = obs[offset:offset + (context["agent_count"] - 1) * stride].reshape(-1, stride)
        offset += (context["agent_count"] - 1) * stride
        if spec.role == "collector":
            if np.any(holding):
                for other in others:
                    if other[4 + int(np.argmax(holding))] > 0:
                        add("deliver_treasure", other[:2], 1 - np.linalg.norm(other[:2]))
            else:
                for target in obs[offset:].reshape(-1, 2 + n):
                    if np.any(target[2:]):
                        add("collect_treasure", target[:2], 1 - np.linalg.norm(target[:2]))
        else:
            deposit_type = context["deposit_types"][spec.agent_id]
            for other in others:
                if other[4 + n + deposit_type] > 0:
                    add("meet_collector", other[:2], 1 - np.linalg.norm(other[:2]))
            collectors = [other[:2] for other in others if not np.any(other[4:4 + n])]
            if collectors:
                add("meet_collector", np.mean(collectors, axis=0), -0.1)
    else:
        raise ValueError(f"No native skill policy for {scenario}")

    if spec.movable:
        if spec.role == "leader_adversary":
            add("coast_and_signal", symbol=0, score=-0.2)
        else:
            add("coast", score=-0.2)
    return candidates
