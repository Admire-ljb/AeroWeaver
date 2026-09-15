import numpy as np
import pytest
import gc

pytest.importorskip("mpe2")
from brain.mpe_policy import local_candidates
from memory.mpe_experience import MPEExperienceMemory
from sim.mpe_catalog import PAPER_SCENARIOS, create_mpe2_environment, role_mapping_for_scenario
from sim.mpe_runtime import MPERuntime


@pytest.mark.parametrize("scenario", PAPER_SCENARIOS)
def test_all_nine_native_observation_reward_and_termination_parity(scenario):
    runtime = MPERuntime(scenario, seed=17, max_cycles=12)
    native = create_mpe2_environment(scenario, max_cycles=12, continuous_actions=False)
    native.reset(seed=17)
    try:
        assert role_mapping_for_scenario(scenario) == {a: s.role for a, s in runtime.specs.items()}
        while not runtime.done:
            row = runtime.policy_step("random_skills")
            obs, rewards, terminated, truncated, _ = native.step(row["actions"])
            assert rewards == row["rewards"]
            assert terminated == row["terminations"]
            assert truncated == row["truncations"]
            for agent in obs:
                np.testing.assert_array_equal(obs[agent], runtime.observations[agent])
                assert native.action_space(agent).contains(row["actions"][agent])
        assert runtime.round == 12
    finally:
        runtime.close()
        native.close()


@pytest.mark.parametrize("scenario", PAPER_SCENARIOS)
def test_heuristic_repeatability_and_role_skill_subsets(scenario):
    hashes = []
    for _ in range(2):
        runtime = MPERuntime(scenario, seed=8, max_cycles=5)
        try:
            for agent, skills in runtime.skills.items():
                assert set(skills.skills).issubset(skills.task_catalog)
                candidates = local_candidates(scenario, runtime.specs[agent], runtime.observations[agent], runtime.public_context)
                for candidate in candidates:
                    assert runtime.env.action_space(agent).contains(skills.dispatch(candidate))
            while not runtime.done:
                runtime.policy_step()
            hashes.append(runtime.trace_hash())
        finally:
            runtime.close()
    assert hashes[0] == hashes[1]


def test_bound_owner_and_native_communication_permissions():
    runtime = MPERuntime("simple_speaker_listener")
    try:
        speaker = runtime.skills["speaker_0"]
        assert set(speaker.skills) == {"signal_goal"}
        assert set(runtime.skills["listener_0"].skills) == {"navigate_to_landmark", "coast"}
        with pytest.raises(ValueError, match="Cross-body"):
            speaker.dispatch({"agent_id": "listener_0", "skill": "signal_goal", "parameters": {"symbol": 1}})
        with pytest.raises(ValueError, match="symbol"):
            speaker.dispatch({"skill": "signal_goal", "parameters": {"symbol": 99}})
        assert runtime.public_context.keys().isdisjoint({"goal", "key", "world_state", "observations"})
    finally:
        runtime.close()


def test_memory_uses_only_completed_same_team_role_records():
    memory = MPEExperienceMemory(gamma=0.5)
    runtime = MPERuntime("simple_tag", max_cycles=2, memory=memory)
    try:
        runtime.policy_step()
        assert memory.records == []
        runtime.policy_step()
        assert len(memory.records) == 8
        owner = runtime.specs["adversary_0"]
        first = next(r for r in memory.records if r["agent_id"] == owner.agent_id)
        assert first["return"] == runtime.trace[0]["rewards"][owner.agent_id] + 0.5 * runtime.trace[1]["rewards"][owner.agent_id]
        memory.records = [dict(first, skill="pursue_target", **{"return": 4.0}),
                          dict(first, skill="coast", **{"return": 0.0}),
                          dict(first, team="evaders", **{"return": 999.0}),
                          dict(first, role="leader_adversary", **{"return": 999.0})]
        assert memory.advantages("simple_tag", owner, np.array(first["state"]), ["pursue_target", "coast", "missing"]) == {
            "pursue_target": 2.0, "coast": -2.0, "missing": 0.0}
    finally:
        runtime.close()


def test_individual_memory_excludes_other_participants():
    memory = MPEExperienceMemory(sharing="individual")
    runtime = MPERuntime("simple_spread", memory=memory, max_cycles=2)
    try:
        while not runtime.done:
            runtime.policy_step()
        memory.records = [r for r in memory.records if r["agent_id"] != "agent_0"]
        assert memory.advantages("simple_spread", runtime.specs["agent_0"], np.zeros(18), ["cover_landmark"]) == {"cover_landmark": 0.0}
    finally:
        runtime.close()


def test_repeated_sdl_lifetimes_and_overlapping_environments():
    import pygame
    other = create_mpe2_environment("simple_spread")
    other.reset(seed=0)
    try:
        for index in range(100):
            env = create_mpe2_environment("simple_tag", max_cycles=1)
            env.reset(seed=index)
            env.step({a: 0 for a in env.agents})
            env.close()
            env.close()
            assert env.unwrapped.game_font is None
            assert pygame.get_init()  # The overlapping environment is still alive.
            del env
            gc.collect()
    finally:
        other.close()
    assert not pygame.get_init()
