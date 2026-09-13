import importlib.util
import json
from pathlib import Path

import pytest

from sim.mock_tasks import SKILL_NAMES, TASKS


ROOT = Path(__file__).resolve().parents[1] / "experiments" / "skill_activation_v1"
spec = importlib.util.spec_from_file_location("activation_pilot", ROOT / "run.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_catalog_matches_executable_skills_and_every_role_has_labels():
    catalog = json.loads((ROOT / "catalog.json").read_text())
    cases = json.loads((ROOT / "cases.json").read_text())
    labels = json.loads((ROOT / "labels.json").read_text())
    pilot.validate_inputs(catalog, cases, labels, SKILL_NAMES, {k: set(v[2]) for k, v in TASKS.items()})
    assert len(catalog) == 22
    assert len(cases) == 18
    assert sum(len(x["roles"]) for x in cases) == 36


def test_alternative_skills_satisfy_one_capability_not_two():
    label = {"required_groups": [["food"], ["hide", "evade"]], "useful": ["food", "hide", "evade"]}
    values = pilot.score_selection(["food", "hide"], label)
    assert values["required_coverage"] == 1
    assert values["complete_coverage"] == 1
    assert pilot.score_selection(["hide", "evade"], label)["required_coverage"] == 0.5
    assert pilot.score_selection([], label)["complete_coverage"] == 0


@pytest.mark.parametrize("content", [
    '{"ranked_skills":["a","a","b","c","d"]}',
    '{"ranked_skills":["a","b"]}',
    '{"ranked_skills":["a","b","c","d","foreign"]}',
    '{"ranked_skills":["a","b","c","d",{}]}',
    'explanation instead of JSON',
])
def test_bad_rankings_are_errors_not_repaired(content):
    with pytest.raises((ValueError, TypeError)):
        pilot.parse_ranking(content, set("abcde"))


def test_model_payload_has_no_gold_or_template():
    case = {"mission": "test", "scenario_id": "coverage", "roles": ["searcher"], "gold": ["secret"]}
    assert pilot.query(case, "searcher") == {"mission": "test", "scenario_id": "coverage", "role": "searcher"}
    assert pilot.parse_ranking('{"ranked_skills":["a","b","c","d","e"]}', set("abcde")) == list("abcde")


def test_macro_average_is_over_scenarios_not_agent_count():
    rows = []
    for scenario, count, value in [("a", 1, 0.0), ("b", 5, 1.0)]:
        for _ in range(count):
            rows.append({"method": "full_catalog", "k": 3, "scenario_id": scenario,
                         "required_coverage": value, "complete_coverage": value,
                         "useful_precision": value, "selected_count": 22,
                         "document_token_proxy": 500, "activation_seconds": 0})
    summary = pilot.aggregate(rows, [])
    assert summary["conditions"]["full_catalog"]["3"]["macro"]["required_coverage"] == 0.5
