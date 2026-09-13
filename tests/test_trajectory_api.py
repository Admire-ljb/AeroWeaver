import json

from flask import Flask
import pytest

from memory.swarm_experience import SwarmExperienceMemory
from memory.trajectory_api import TrajectoryBrowser, register_trajectory_api


@pytest.fixture
def memory(tmp_path):
    store = SwarmExperienceMemory(tmp_path / "memory.sqlite3", max_records=2)
    yield store
    store.close()


def add(memory, episode="ep1", agent="UAV_1", reward=0, success=True, role="pursuer", reuse=False):
    return memory.record_step(mission_id=episode, task="pursuit", state={"x": 0},
        next_state={"x": 1}, action_id="move", skill="pursue_target", agent_id=agent,
        role=role, success=success, reward=reward, reward_source="mock_native_reward",
        invocation={"skill": "pursue_target"},
        metadata={"reuse_allowed": reuse, "experiment_id": "pilot", "condition": "llm"})


def test_browser_is_not_limited_by_retrieval_capacity_or_write_only(memory):
    ids = [add(memory, episode=f"ep{i}") for i in range(5)]
    browser = TrajectoryBrowser(memory.path)
    assert len(memory.records()) == 2
    assert browser.stats()["episodes"] == 5
    assert browser.episodes(limit=2)["total"] == 5
    page1 = browser.episodes(limit=2)["items"]
    page2 = browser.episodes(limit=2, offset=2)["items"]
    assert not {r["episode_id"] for r in page1} & {r["episode_id"] for r in page2}
    assert browser.transition(ids[0])["metadata"]["reuse_allowed"] is False
    assert browser.transitions()["total"] == 5


def test_episode_agent_role_search_failure_and_literal_wildcards(memory):
    add(memory, agent="UAV_1")
    add(memory, agent="UAV_2", role="evader", success=False)
    add(memory, episode="ep2", agent="UAVx1")
    browser = TrajectoryBrowser(memory.path)
    assert browser.transitions(query="UAV_1")["total"] == 1
    assert browser.transitions(query="%")["total"] == 0
    assert browser.transitions(query="' OR 1=1 --")["total"] == 0
    assert browser.transitions(episode="ep1", role="evader", agent="UAV_2", failed=True)["total"] == 1
    assert browser.transitions(episode="ep2", failed=True)["total"] == 0
    assert browser.episodes(query="evader")["items"][0]["records"] == 2
    assert len(browser.episode("ep1")["agents"]) == 2
    assert browser.episode("absent") is None


def test_zero_unknown_rewards_and_finalized_returns_come_from_sql_columns(memory):
    first = add(memory, reward=0)
    add(memory, reward=2)
    add(memory, episode="unknown", reward=None)
    memory.finalize_mission(mission_id="ep1", success=False)
    browser = TrajectoryBrowser(memory.path)
    row = browser.transition(first)
    assert row["immediate_reward"] == 0
    assert row["return_value"] == pytest.approx(1.9)
    assert row["return_finalized"] is True
    assert row["episode_id"] == "ep1"
    assert browser.stats()["zero_reward_records"] == 1
    assert browser.stats()["rewarded_records"] == 2
    assert browser.stats()["pending_returns"] == 1
    assert browser.transitions(episode="unknown")["items"][0]["return_value"] is None


def test_inspection_does_not_mutate_memory_or_call_retrieval(memory):
    add(memory, success=False)
    before = memory._db.execute("SELECT * FROM transitions").fetchall()
    browser = TrajectoryBrowser(memory.path)
    browser.episodes(); browser.stats(); browser.transitions(failed=True)
    after = memory._db.execute("SELECT * FROM transitions").fetchall()
    assert [tuple(row) for row in before] == [tuple(row) for row in after]


def test_http_routes_errors_and_unavailable_store(memory):
    record_id = add(memory)
    app = Flask(__name__)
    register_trajectory_api(app, lambda: memory)
    client = app.test_client()
    assert client.get("/api/memory/trajectory-stats").json["stats"]["records"] == 1
    assert client.get("/api/memory/episodes").json["items"][0]["episode_id"] == "ep1"
    assert client.get("/api/memory/episodes/ep1").json["agents"][0]["agent_id"] == "UAV_1"
    assert client.get("/api/memory/transitions?episode_id=ep1").json["total"] == 1
    assert client.get("/api/memory/transitions/" + record_id).json["record"]["next_state"] == {"x": 1}
    for suffix in ("limit=0", "offset=-1", "limit=abc", "limit=201"):
        assert client.get("/api/memory/episodes?" + suffix).status_code == 400
    assert client.get("/api/memory/episodes/absent").status_code == 404
    assert client.get("/api/memory/transitions/absent").status_code == 404
    unavailable = Flask("unavailable")
    register_trajectory_api(unavailable, lambda: None)
    response = unavailable.test_client().get("/api/memory/trajectory-stats")
    assert response.status_code == 503 and response.json["ok"] is False
