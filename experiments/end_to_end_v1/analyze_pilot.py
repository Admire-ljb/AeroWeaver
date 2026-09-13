"""Verify recorded development evidence without promoting it to main results."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import sqlite3


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    output = args.run / "remote-artifacts/outputs"
    plan = load(output / "plan.json")
    completion = load(output / "completion.json")
    single_episode = plan.get("single_episode", False)
    result_split = "single_episode" if single_episode else "development_probe"
    rows = [load(p) for p in sorted((output / "episodes").glob("*/summary.json"))]
    violations = []
    all_records = {}
    trajectories = defaultdict(list)
    for path in (output / "memory").glob("*.sqlite3"):
        if path.name.endswith("-snapshot.sqlite3"):
            continue
        with sqlite3.connect(path) as db:
            for rid, mission, agent, step, reward, value, finalized, payload in db.execute(
                    "SELECT id,mission,agent,step,reward,return_value,finalized,payload FROM transitions"):
                record = json.loads(payload)
                record.update(return_value=value, finalized=bool(finalized))
                all_records[rid] = record
                trajectories[mission, agent].append((step, reward, value, bool(finalized)))
    discount_checked = 0
    for (mission, agent), entries in trajectories.items():
        entries.sort()
        total = 0.0
        next_step = entries[-1][0] + 1
        for step, reward, value, finalized in reversed(entries):
            total = reward + plan["gamma"] ** (next_step - step) * total
            if not math.isclose(total, value, rel_tol=1e-9, abs_tol=1e-9):
                violations.append(f"Discount mismatch {mission} {agent} {step}")
            next_step = step
            discount_checked += 1
        if [row[0] for row in entries] != list(range(len(entries))):
            violations.append(f"Noncontiguous trajectory {mission} {agent}")
    initial_hashes = defaultdict(set)
    for row in rows:
        if row.get("initial_state_sha256"):
            initial_hashes[row["scenario"], row["seed"]].add(row["initial_state_sha256"])
        expected = sum(len(v) for (mission, _), v in trajectories.items() if mission == row["episode_id"])
        if row["records"] != expected:
            violations.append(f"Summary count mismatch {row['episode_id']}")
    if any(len(values) != 1 for values in initial_hashes.values()):
        violations.append("Initial state mismatch across paired methods")
    ranking_counts = Counter()
    error_counts = Counter()
    review_error_counts = Counter()
    retrievals = 0
    cross_episode_retrievals = 0
    for path in sorted((output / "episodes").glob("*/trace.jsonl")):
        summary = load(path.parent / "summary.json")
        episode = summary["episode_id"]
        if not (path.parent / "initial_state.json").exists():
            if path.stat().st_size:
                violations.append(f"Trace without initial state: {episode}")
            continue
        roles = summary.get("roles") or load(path.parent / "initial_state.json")["roles"]
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            for agent, decision in event["decisions"].items():
                if decision.get("error"):
                    error_counts[decision["error"]] += 1
                review_error = (decision.get("local_review") or {}).get("review_error")
                if review_error:
                    review_error_counts[review_error] += 1
                ranking = decision.get("ranking")
                if not ranking:
                    continue
                ranking_counts["decisions"] += 1
                ranking_counts["changed"] += ranking["changed_by_memory"]
                ranking_counts["uncertified"] += not ranking["certified"]
                ranking_counts["missing_some_scores"] += bool(ranking["missing_scores"])
                retrieved = ranking["retrieved"]
                mean_return = sum(r["return_observed_at_decision"] for r in retrieved) / len(retrieved) if retrieved else 0.0
                for record in retrieved:
                    retrievals += 1
                    if record["mission"] != episode:
                        cross_episode_retrievals += 1
                    if record["role"] != roles[agent]:
                        violations.append(f"Role leakage {episode} {agent}")
                    if record["mission"] == episode and record["step"] >= event["step"]:
                        violations.append(f"Future step leakage {episode} {event['step']}")
                    if record["id"] not in all_records or not all_records[record["id"]]["metadata"]["reuse_allowed"]:
                        violations.append(f"Unpermitted retrieval {record['id']}")
                for label, option in ranking["candidate_map"].items():
                    same = [r["return_observed_at_decision"] for r in retrieved if r["skill"] == option["skill"]]
                    advantage = sum(same) / len(same) - mean_return if same else 0.0
                    if not math.isclose(advantage, ranking["advantages"][label], abs_tol=1e-9):
                        violations.append(f"Advantage mismatch {episode} {event['step']} {agent}")
                for label, base in ranking["base_scores"].items():
                    expected = base + ranking["beta"] * ranking["advantages"][label]
                    if not math.isclose(expected, ranking["updated_observed_scores"][label], abs_tol=1e-9):
                        violations.append(f"Score update mismatch {episode} {event['step']} {agent}")
    usage = Counter()
    invalid_actions = []
    truncated_calls = []
    for line in (output / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        usage["calls"] += 1
        usage["recorded_tokens"] += call.get("usage", {}).get("total_tokens", 0)
        usage["call_errors"] += bool(call.get("error"))
        choices = call.get("response", {}).get("choices", [])
        if choices and choices[0].get("finish_reason") != "stop":
            truncated_calls.append({"episode": call["episode_id"], "step": call.get("step"),
                "phase": call["phase"], "finish_reason": choices[0].get("finish_reason"),
                "max_tokens": call["request"].get("max_tokens")})
        executed_plan = call.get("phase") == "central_revision" or (
            call.get("phase") == "central_plan" and call.get("condition") == "central_api")
        if not executed_plan or call.get("error") or not choices:
            continue
        data = json.loads(call["request"]["messages"][-1]["content"])
        try:
            response = json.loads(choices[0]["message"]["content"])
            actions = response.get("actions", {})
            if not isinstance(actions, dict):
                raise ValueError("Actions are not an object")
        except (ValueError, AttributeError):
            invalid_actions.append({"episode": call["episode_id"], "step": call["step"],
                                    "reason": "malformed_joint_response"})
            continue
        for agent, obs in data["team_observations"].items():
            options = data.get("executable_actions_by_body", {}).get(agent, obs.get("options", []))
            action = actions.get(agent)
            if action in options:
                continue
            reason = "missing_or_malformed_action"
            if isinstance(action, dict):
                matching = [o for o in options if o["skill"] == action.get("skill")]
                reason = "skill_not_in_own_current_options" if not matching else "parameters_not_in_own_current_options"
            invalid_actions.append({"episode": call["episode_id"], "step": call["step"], "body": agent,
                                   "reason": reason, "action": action, "allowed": options})
    checks = {"verification_status": "ANALYZED", "scope": "record consistency, not independent stochastic reproduction",
              "violations": violations, "recorded_transitions": len(all_records),
              "discount_values_checked": discount_checked, "paired_initial_state_groups": len(initial_hashes),
              "retrieved_records_checked": retrievals, "cross_episode_retrievals": cross_episode_retrievals,
              "ranking": dict(ranking_counts), "usage": dict(usage), "decision_error_types": dict(error_counts),
              "review_error_types": dict(review_error_counts), "interface_version": plan.get("interface_version", "v1")}
    checks["invalid_action_reasons"] = dict(Counter(row["reason"] for row in invalid_actions))
    checks["truncated_calls"] = truncated_calls
    (args.run / "invalid_actions.json").write_text(json.dumps(invalid_actions, indent=2) + "\n", encoding="utf-8")
    (args.run / "audit.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    lines = ["# End-to-End Development Pilot", "", "## Material Passport",
             "- Origin skill: academic-research-suite / experiment-agent.",
             "- Verification status: ANALYZED (trace consistency; no independent performance reproduction).",
             "- This is the LLM subset of a development pilot, not the five-method main experiment.",
             "", "## Execution", f"- Finished episodes: {len(rows)} / {plan['planned_episodes']}.",
             f"- Technical failures: {completion['technical_failures']}.",
             f"- Provider calls: {usage['calls']}; reported tokens: {usage['recorded_tokens']:,}.",
             f"- Saved transitions: {len(all_records)}; discounted returns checked: {discount_checked}.",
             f"- Audit violations: {len(violations)}.",
             f"- Runtime sources unchanged: {completion['source_hashes_unchanged']}.",
             "- Production app was not restarted; experience is in isolated experiment SQLite files.",
             "", "## Single Episodes" if single_episode else "## Probe Episodes",
             "One cold-start episode per task/method." if single_episode else "One probe episode per task/method.", "",
             "| Task | Method | Status | Team return | Output/execution errors | Tokens | Median team decision (s) |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for row in rows:
        if row["split"] != result_split:
            continue
        value = f"{row['controlled_team_mean_return']:.3f}" if "controlled_team_mean_return" in row else "N/A"
        latency = f"{row.get('p50_decision_latency_s', 0):.3f}"
        lines.append(f"| {row['scenario']} | {row['method']} | {row['status']} | {value} | {row['decision_errors']} / {row['invocation_errors']} | {row.get('tokens', 0)} | {latency} |")
    lines += ["", "## Experience Path",
        f"- Log-probability ranking decisions: {ranking_counts['decisions']}.",
        f"- Decisions changed by the recorded reward correction: {ranking_counts['changed']}.",
        f"- Decisions with incomplete probability coverage: {ranking_counts['missing_some_scores']}.",
        f"- Uncertified censored rankings using the base choice: {ranking_counts['uncertified']}.",
        f"- Retrieval references to earlier episodes: {cross_episode_retrievals}.",
        "- Retrieved return values were logged at decision time; final database returns can differ after later rewards arrive.",
        "", "## Limits",
        "- MAPPO and MADDPG were not run: adapters/trained checkpoints are not available.",
        ("- One cold-start episode; within-episode memory only, no cross-episode adaptation evaluation."
         if single_episode else "- One adaptation episode and one probe episode.") + " 24 decision rounds, at most 12 simulated seconds.",
        "- HMAS-2 is adapted with one local feedback round, not an unchanged reproduction of the original task setup.",
        "- Source dynamics are Mock; rewards are MPE2 functions evaluated on mapped Mock state.",
        "- Skill executors contain control heuristics. Results do not establish superiority over learned flight control.",
        "- Invalid model actions use the recorded hold fallback; affected returns must not be interpreted as a clean capability comparison.",
        "- No confidence interval or significance claim is appropriate for these single-seed diagnostics.",
        "- Earlier 402 and JSON-mode 400 attempts, and the offline deepcopy exception, are separate retained development records.",
        "", "## Decision Errors"]
    lines.extend(f"- {count}: `{error}`" for error, count in error_counts.items())
    if not error_counts:
        lines.append("- No executed-decision fallback errors observed in this batch.")
    lines.extend(f"- Local review error ({count}): `{error}`" for error, count in review_error_counts.items())
    lines += ["", "## Interface Diagnosis",
        f"- Invalid final plan actions recorded: {len(invalid_actions)}. See invalid_actions.json for body-local options.",
        f"- Incomplete provider outputs: {len(truncated_calls)}; their phases and token limits are preserved in audit.json.",
        "- Do not silently repair or exclude affected results. Protocol changes require a separately identified development batch."]
    coverage = {r["method"]: r for r in rows if r["scenario"] == "coverage" and r["split"] == result_split}
    if coverage.get("aeroweaver_pilot", {}).get("status") == "horizon":
        lines.append("- AeroWeaver did not complete coverage within the short horizon. Interface validity is not task success.")
    gate_path = args.run / "remote-artifacts/calibration/gate.json"
    if gate_path.exists():
        gate = load(gate_path)
        lines += ["", "## Saved-State Calibration",
                  f"- Gate passed: {gate['passed']}; saved cases: {gate['cases']}.",
                  f"- Additional calibration calls: {gate['calls']}; reported tokens: {gate['tokens']:,}.",
                  "- Deliberately selected saved-state regressions are not an unbiased accuracy sample.",
                  "- New episode seeds differ from the parent batch, so returns are not a paired before/after performance comparison."]
    (args.run / "PILOT_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    hashes = {str(p.relative_to(args.run)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in args.run.rglob("*") if p.is_file() and p.name != "artifact_hashes.json"}
    (args.run / "artifact_hashes.json").write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"episodes": len(rows), "audit": checks}, indent=2))


if __name__ == "__main__":
    main()
