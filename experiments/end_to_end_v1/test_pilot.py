"""No-network checks for the pilot's scoring and execution bookkeeping."""

import json
from pathlib import Path

import pytest
import threading

import pilot


def test_single_episode_schedule_has_exactly_nine_paired_episodes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = []

    def fake_episode(scenario, method, seed, split, output, skills, client, memory_path, rounds):
        assert not memory_path.exists()
        calls.append((scenario, method, seed, split))
        return {"status": "horizon"}

    monkeypatch.setattr(pilot, "run_episode", fake_episode)
    for scenario in pilot.support.SCENARIOS:
        for method in pilot.METHODS:
            rows = pilot.run_stream(scenario, method, tmp_path, [], SimpleNamespace(stop=threading.Event()),
                                    24, pilot.episode_schedule(True, 65001))
            assert len(rows) == 1
    assert len(calls) == len(set(calls)) == 9
    assert {row[2:] for row in calls} == {(65001, "single_episode")}


def test_default_schedule_is_unchanged():
    assert pilot.episode_schedule() == (("development_adaptation", 64011), ("development_probe", 64012))


def test_existing_memory_is_not_reused(tmp_path):
    memory = tmp_path / "memory/coverage-central_api.sqlite3"
    memory.parent.mkdir()
    memory.touch()
    with pytest.raises(FileExistsError):
        pilot.run_stream("coverage", "central_api", tmp_path, [], None, 24, pilot.episode_schedule(True))


def response(scores, selected="A"):
    rows = [{"token": name, "logprob": value} for name, value in scores.items()]
    return {"message": {"content": selected}, "logprobs": {"content": [
        {"token": selected, "logprob": scores[selected], "top_logprobs": rows}]}}


def test_complete_ranking():
    selected, detail = pilot.corrected_selection(response({"A": -1, "B": -2}), ["A", "B"], {"A": 0, "B": 3})
    assert selected == "B" and detail["certified"] and detail["changed_by_memory"]


def test_beta_zero():
    selected, detail = pilot.corrected_selection(response({"A": -1, "B": -2}), ["A", "B"], {"B": 30}, beta=0)
    assert selected == "A" and not detail["changed_by_memory"]


def test_no_imputation_or_uncertified_flip():
    selected, detail = pilot.corrected_selection(response({"A": -1, "B": -2}), ["A", "B", "C"], {"B": 3, "C": 10})
    assert selected == "A" and not detail["certified"] and "C" not in detail["base_scores"]


def test_censoring_certificate():
    scores = {"A": -1, "B": -2, **{f"other{i}": -10 - i for i in range(18)}}
    selected, detail = pilot.corrected_selection(response(scores), ["A", "B", "C"], {"B": 3, "C": 0})
    assert selected == "B" and detail["certified"] and detail["missing_scores"] == ["C"]


def test_invalid_action_does_not_become_index_fallback():
    with pytest.raises(ValueError):
        pilot.strict_action({"option_index": 0}, {"options": [pilot.STOP]})


class StubClient:
    def request(self, messages, context, max_tokens=300, logits=False):
        data = json.loads(messages[-1]["content"])
        if logits:
            labels = list(data["options"])
            choice = response({label: -1 - i for i, label in enumerate(labels)}, labels[0])
        else:
            assert "json" in messages[0]["content"].lower(), "Provider JSON mode requires an explicit JSON instruction"
            if context["phase"] == "activation":
                result = {"skills": ["cover_landmark", "pursue_target", "follow_peer", "evade_peer",
                                      "signal_target", "return_in_bounds", "explore_local", "hold_position"]}
            elif context["phase"] == "local_review":
                result = {"accept": True, "feedback": ""}
            else:
                result = {"actions": {rid: options[0] for rid, options in data["executable_actions_by_body"].items()}}
            choice = {"message": {"content": json.dumps(result)}}
        return {"choices": [choice]}


@pytest.mark.parametrize("scenario", pilot.support.SCENARIOS)
@pytest.mark.parametrize("method", pilot.METHODS)
def test_episode_bookkeeping(tmp_path, scenario, method):
    skills = json.loads((Path(__file__).parent / "catalog.json").read_text())
    memory = tmp_path / "memory.sqlite3"
    result = pilot.run_episode(scenario, method, 63001, "synthetic_preflight", tmp_path,
                               skills, StubClient(), memory, rounds=12)
    assert result["status"] in {"horizon", "complete"}, result.get("traceback")
    assert result["decision_errors"] == 0
    assert result["invocation_errors"] == 0
    db = pilot.support.SwarmExperienceMemory(memory)
    try:
        records = db.records(result["episode_id"])
        assert len(records) == result["records"]
        assert all(r.return_finalized for r in records)
        assert {r.role for r in records} == set(result["roles"].values())
        if method == "aeroweaver_pilot":
            controlled = [r for r in records if r.role not in pilot.support.FIXED_ROLES[scenario]]
            assert all(r.metadata["reuse_allowed"] for r in controlled)
            assert result["retrieval_decisions"] > 0
    finally:
        db.close()


def test_bad_local_review_does_not_stop_all_bodies(tmp_path):
    class FailedReviewer(StubClient):
        def request(self, messages, context, max_tokens=300, logits=False):
            if context["phase"] == "local_review" and context["agent_id"] == "UAV_1":
                raise ValueError("Incomplete response; test fixture")
            return super().request(messages, context, max_tokens, logits)
    task = pilot.support.ActivatedTask("coverage", ["cover_landmark", "hold_position"], seed=63001)
    skills = json.loads((Path(__file__).parent / "catalog.json").read_text())
    result = pilot.decisions(FailedReviewer(), task, {rid: task.observe(rid) for rid in task.roles},
                             skills, None, "review-error-fixture", "hmas2_adapted")
    assert result["UAV_1"]["local_review"]["review_error"]
    assert all(row["error"] is None for row in result.values())
    assert all(row["choice"]["skill"] == "cover_landmark" for row in result.values())


def test_body_options_stay_separate_from_catalog():
    class InspectClient(StubClient):
        def request(self, messages, context, max_tokens=300, logits=False):
            data = json.loads(messages[-1]["content"])
            assert data["executable_actions_by_body"]["UAV_1"] == [pilot.STOP]
            assert data["executable_actions_by_body"]["UAV_2"][0]["skill"] == "pursue_target"
            assert "options" not in data["team_observations"]["UAV_1"]
            assert max_tokens == pilot.CENTRAL_MAX_TOKENS
            return super().request(messages, context, max_tokens, logits)
    from types import SimpleNamespace
    task = SimpleNamespace(task_id="pursuit", roles={"UAV_1": "pursuer", "UAV_2": "pursuer"})
    obs = {"UAV_1": {"options": [pilot.STOP]}, "UAV_2": {"options": [
        {"skill": "pursue_target", "parameters": {"target_id": "UAV_4"}}]}}
    result = pilot.central_plan(InspectClient(), task, obs, [], {"episode_id": "fixture"})
    assert result["actions"]["UAV_1"] == pilot.STOP
