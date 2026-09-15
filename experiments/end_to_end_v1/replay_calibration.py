"""Prepare fixed interface regressions, then replay new prompts without physics."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
from types import SimpleNamespace


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def prepare(source, output):
    grouped = defaultdict(list)
    for line in (source / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        if "-development_adaptation-" not in call["episode_id"] or call.get("error"):
            continue
        method = call["condition"]
        final = call["phase"] == "central_revision" if method == "hmas2_adapted" else call["phase"] == "central_plan"
        if method not in {"central_api", "hmas2_adapted"} or not final:
            continue
        data = json.loads(call["request"]["messages"][-1]["content"])
        actions = json.loads(call["response"]["choices"][0]["message"]["content"]).get("actions", {})
        observations = data["team_observations"]
        invalid = sum(actions.get(rid) not in obs["options"] for rid, obs in observations.items())
        scenario = call["scenario"]
        if scenario != "coverage" and invalid == 0:
            continue
        roles = json.loads((source / "episodes" / call["episode_id"] / "initial_state.json").read_text())["roles"]
        grouped[scenario, method].append({"case_id": f"{scenario}-{method}-step{call['step']}",
            "scenario": scenario, "method": method, "roles": roles, "observations": observations,
            "source_episode": call["episode_id"], "source_step": call["step"],
            "old_actions": actions, "old_invalid_actions": invalid})
    cases = []
    for (scenario, method), candidates in sorted(grouped.items()):
        cases.extend(sorted(candidates, key=lambda row: row["source_step"])[:1 if scenario == "coverage" else 3])
    assert len(cases) == 14, "Expected all six task/method groups"
    save(output, {"source": str(source), "scope": "targeted regression cases, not an unbiased accuracy sample",
                  "selection": "first three invalid adaptation steps per affected task/method; first coverage step as control",
                  "cases": cases})
    print(json.dumps({"prepared_cases": len(cases), "old_invalid_actions": sum(c["old_invalid_actions"] for c in cases)}))


def replay(cases_path, catalog_path, output):
    import pilot
    output.mkdir(parents=True, exist_ok=False)
    cases = json.loads(cases_path.read_text())["cases"]
    skills = json.loads(catalog_path.read_text())
    client = pilot.Client(output / "calls.jsonl", call_limit=96, token_limit=300000)
    results = []
    for case in cases:
        task = SimpleNamespace(task_id=case["scenario"], roles=case["roles"], round=case["source_step"])
        try:
            decisions = pilot.decisions(client, task, case["observations"], skills, None,
                                        "regression-" + case["case_id"], case["method"])
            invalid = sum(bool(row["error"]) for row in decisions.values())
            reviews = sum(bool((row.get("local_review") or {}).get("review_error")) for row in decisions.values())
            result = {"case_id": case["case_id"], "old_invalid": case["old_invalid_actions"],
                      "new_invalid": invalid, "review_errors": reviews, "decisions": decisions}
        except Exception as exc:
            result = {"case_id": case["case_id"], "technical_error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        save(output / "progress.json", results)
        print(json.dumps({k: v for k, v in result.items() if k != "decisions"}), flush=True)
        if client.stop.is_set():
            break
    errors = sum(bool(json.loads(line).get("error")) for line in (output / "calls.jsonl").read_text().splitlines())
    passed = len(results) == len(cases) and errors == 0 and all(
        not r.get("technical_error") and r["new_invalid"] == 0 and r["review_errors"] == 0 for r in results)
    save(output / "gate.json", {"passed": passed, "cases": len(results), "planned": len(cases),
         "calls": client.calls, "tokens": client.tokens, "call_errors": errors,
         "interface_version": pilot.INTERFACE_VERSION, "results": results})
    print(json.dumps({"regression_gate_passed": passed, "calls": client.calls, "tokens": client.tokens}), flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare, args.output)
        return 0
    return replay(args.cases, args.catalog, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
