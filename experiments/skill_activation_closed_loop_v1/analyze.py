"""Describe the frozen first batch without removing failed episodes."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics


def quantile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    output = root / "remote-artifacts/outputs"
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    calls = [json.loads(line) for line in (output / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    by_episode = defaultdict(list)
    for call in calls:
        by_episode[call["episode_id"]].append(call)
    rows, failures, sources = [], [], Counter()
    conditions, role_rows, coverage_rows, paired_rows = defaultdict(list), [], [], []
    for episode in report["episodes"]:
        folder = output / "episodes" / episode["episode_id"]
        episode_calls = by_episode[episode["episode_id"]]
        decision_calls = [call for call in episode_calls if call["phase"] == "decision"]
        activation_calls = [call for call in episode_calls if call["phase"] == "activation"]
        latencies = [call["latency_s"] for call in decision_calls]
        row = {key: episode.get(key) for key in (
            "episode_id", "scenario_id", "condition", "seed", "status", "success", "rounds", "records",
            "active_count", "decision_errors", "invocation_errors", "peer_messages", "total_tokens",
            "prompt_tokens", "completion_tokens", "usage_missing_requests", "duration_s")}
        row.update(decision_requests=len(decision_calls), activation_requests=len(activation_calls),
            decision_total_tokens=sum((call.get("usage") or {}).get("total_tokens", 0) for call in decision_calls),
            activation_total_tokens=sum((call.get("usage") or {}).get("total_tokens", 0) for call in activation_calls),
            mean_decision_latency_s=statistics.mean(latencies) if latencies else None,
            p95_decision_latency_s=quantile(latencies, .95),
            mean_tokens_per_decision=statistics.mean((call.get("usage") or {}).get("total_tokens", 0) for call in decision_calls) if decision_calls else None)
        for role, value in episode.get("mean_return_by_role", {}).items():
            role_rows.append({"episode_id": episode["episode_id"], "scenario_id": episode["scenario_id"],
                "condition": episode["condition"], "seed": episode["seed"], "role": role,
                "fixed_opponent": role in episode["fixed_opponent_roles"], "mean_agent_return": value})
        activation_path = folder / "activation.json"
        if activation_path.exists():
            activation = json.loads(activation_path.read_text(encoding="utf-8"))
            coverage_rows.append({"episode_id": episode["episode_id"], "scenario_id": episode["scenario_id"],
                "condition": episode["condition"], "seed": episode["seed"],
                "active_count": len(activation["active_with_common_stop"]),
                "engineering_template_coverage": episode.get("diagnostic_template_skill_coverage"),
                "selected_skills": ";".join(activation["active_with_common_stop"])})
        trace_path = folder / "trace.jsonl"
        if trace_path.exists():
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                interval = json.loads(line)
                for agent, decision in interval["decisions"].items():
                    sources[decision["source"]] += 1
                    if decision.get("error") or decision.get("execution_error"):
                        matching = [call for call in decision_calls if call.get("step") == interval["step"] and call.get("agent_id") == agent]
                        raw = matching[0].get("response", {}) if matching else {}
                        failure = {"episode_id": episode["episode_id"], "scenario_id": episode["scenario_id"],
                            "condition": episode["condition"], "seed": episode["seed"], "step": interval["step"],
                            "agent_id": agent, "error": decision.get("error") or decision.get("execution_error"),
                            "actual_skill": decision["choice"]["skill"],
                            "option_count": len(interval["observations"][agent]["options"]),
                            "raw_model_content": ((raw.get("choices") or [{}])[0].get("message") or {}).get("content")}
                        failures.append(failure)
        conditions[(episode["scenario_id"], episode["condition"])].append(row)
        rows.append(row)
    aggregate = []
    for (scenario, condition), episodes in sorted(conditions.items()):
        group_calls = [call for call in calls if call["scenario_id"] == scenario and call["condition"] == condition and call["phase"] == "decision"]
        latencies = [call["latency_s"] for call in group_calls]
        aggregate.append({"scenario": scenario, "condition": condition, "episodes": len(episodes),
            "successes": None if scenario == "world_communication" else sum(row["success"] is True for row in episodes),
            "technical_failures": sum(row["status"] == "technical_failure" for row in episodes),
            "mean_rounds": statistics.mean(row["rounds"] for row in episodes),
            "mean_active_skills": statistics.mean(row["active_count"] for row in episodes),
            "mean_episode_tokens": statistics.mean(row["total_tokens"] for row in episodes),
            "mean_decision_tokens": statistics.mean((call.get("usage") or {}).get("total_tokens", 0) for call in group_calls),
            "mean_decision_latency_s": statistics.mean(latencies), "p95_decision_latency_s": quantile(latencies, .95),
            "decision_errors": sum(row["decision_errors"] for row in episodes),
            "invocation_errors": sum(row["invocation_errors"] for row in episodes)})
    index = {(r["scenario_id"], r["condition"], r["seed"]): r for r in rows}
    for (scenario, method, seed), row in index.items():
        if method == "full_catalog":
            continue
        reference = index[(scenario, "full_catalog", seed)]
        paired_rows.append({"scenario": scenario, "condition": method, "seed": seed,
            "episode_token_delta_vs_full": row["total_tokens"] - reference["total_tokens"],
            "decision_token_delta_vs_full": row["mean_tokens_per_decision"] - reference["mean_tokens_per_decision"],
            "success_delta_vs_full": None if row["success"] is None else int(row["success"] is True) - int(reference["success"] is True)})
    write_csv(root / "episode_metrics.csv", rows)
    write_csv(root / "aggregate_metrics.csv", aggregate)
    write_csv(root / "role_returns.csv", role_rows)
    write_csv(root / "activation_diagnostics.csv", coverage_rows)
    write_csv(root / "paired_differences.csv", paired_rows)
    if failures:
        write_csv(root / "decision_failures.csv", failures)
    boundary_index_errors = 0
    for failure in failures:
        try:
            returned = json.loads(failure["raw_model_content"])["option_index"]
            boundary_index_errors += isinstance(returned, int) and returned == failure["option_count"]
        except (ValueError, TypeError, KeyError):
            pass
    analysis = {"episodes": len(rows), "records": sum(row["records"] for row in rows),
        "requests": len(calls), "total_tokens": sum(row["total_tokens"] for row in rows),
        "usage_missing_requests": sum(call.get("usage") is None for call in calls),
        "api_errors": sum(bool(call.get("error")) for call in calls),
        "decision_sources": dict(sources), "failures": failures, "aggregate": aggregate,
        "returned_index_equals_option_count": boundary_index_errors,
        "verification": report["verification"], "model_ids": sorted({call.get("response", {}).get("model", "unknown") for call in calls})}
    (root / "analysis.json").write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    labels = {"full_catalog": "Full catalog", "lexical_topk": "BM25 activation", "semantic_topk": "Semantic activation"}
    lines = ["# First-Round Closed-Loop Skill Activation Report", "", "## Material Passport",
        "- Workflow: academic-research-suite / experiment-agent.",
        "- Status: observed development batch; not held-out efficacy validation.",
        f"- Experiment: `{report['experiment_id']}`.",
        "- Host: 10.61.3.7. Existing web service was not restarted or used for simulation.", "",
        "## Protocol", "",
        "The batch compares full-catalog planning, whole-mission BM25 top-6 activation, and whole-mission LLM top-6 activation across coverage, pursuit, and world communication, with five paired environment seeds per task. All conditions retain the same own-body hold action and executor preconditions. Fixed evader/forager policies isolate the evaluated cooperative team. The initial worlds, local observations, peer messaging, role assignments, fixed executors, and reward equations are shared across conditions.", "",
        "The backbone is official deepseek-v4-flash in non-thinking mode. Physics waits for all body-local decisions and then advances ten existing Mock ticks, equal to 0.5 simulated seconds. Each episode has a maximum of 24 intervals (12 simulated seconds). This measures controlled decision quality and inference usage, not real-time latency tolerance or general long-horizon task success.", "",
        "Rewards are computed using MPE2 1.1.0 scenario reward functions on mapped Mock state. They are not native MPE rollouts. Returns use gamma=0.95 and remain separated by episode, participant and role. All records are write-only for this batch; online updates and experience reuse are disabled.", "",
        "## Observed Results", "",
        "| Task | Condition | Successes | Mean active skills | Mean episode tokens | Mean tokens / decision | Decision errors |",
        "|---|---|---:|---:|---:|---:|---:|"]
    for row in aggregate:
        successes = "N/A" if row["successes"] is None else f"{row['successes']}/{row['episodes']}"
        lines.append(f"| {row['scenario']} | {labels[row['condition']]} | {successes} | {row['mean_active_skills']:.1f} | {row['mean_episode_tokens']:.0f} | {row['mean_decision_tokens']:.0f} | {row['decision_errors']} |")
    lines += ["", "### Main Observations", ""]
    by_condition = {(row["scenario"], row["condition"]): row for row in aggregate}
    for scenario in ("coverage", "pursuit", "world_communication"):
        full = by_condition[(scenario, "full_catalog")]
        semantic = by_condition[(scenario, "semantic_topk")]
        reduction = (1 - semantic["mean_episode_tokens"] / full["mean_episode_tokens"]) * 100
        lines.append(f"- {scenario}: semantic activation used {reduction:.1f}% fewer mean episode tokens than the full catalog, including its activation request.")
    lines += ["- Coverage reached completion in 1/5 episodes for both full-catalog and semantic activation, and 0/5 for lexical activation.",
        "- No pursuit condition reached the existing capture criterion within the 12-second simulated horizon. Positive MPE2-derived contact rewards are not the same as the Mock two-pursuer capture criterion.",
        "- In world communication, both activation conditions had lower cooperative leader/pursuer mean returns than the full catalog. The results do not support a claim of uniformly improved task performance."]
    lines += ["", "Episode tokens include semantic activation where applicable and every subsequent model request. Per-decision costs are also provided to distinguish shorter episodes from cheaper individual decisions. Latencies include shared provider/concurrency effects and are descriptive.", "",
        "World communication has no existing binary success criterion; its outcomes are reported by role without introducing a post-hoc success label.", "",
        "| Task | Condition | Role | Mean cumulative return |", "|---|---|---|---:|"]
    grouped_roles = defaultdict(list)
    for row in role_rows:
        grouped_roles[(row["scenario_id"], row["condition"], row["role"])].append(row["mean_agent_return"])
    for (scenario, method, role), values in sorted(grouped_roles.items()):
        lines.append(f"| {scenario} | {labels[method]} | {role} | {statistics.mean(values):.3f} |")
    lines += ["", "## Records and Verification", "",
        f"- Completed batch entries: {len(rows)}; recorded transitions: {analysis['records']}.",
        f"- Provider requests: {len(calls)}; returned total tokens: {analysis['total_tokens']}; missing usage: {analysis['usage_missing_requests']}.",
        f"- HTTP/parsing/API-call errors: {analysis['api_errors']}; logged decision/execution failures: {len(failures)}.",
        f"- Decision sources: `{json.dumps(dict(sources), sort_keys=True)}`.",
        f"- Verification: `{json.dumps(report['verification'], sort_keys=True)}`.",
        "- Failed requests were not retried or removed. Invalid decisions used the recorded common own-body hold; rewards are associated with this executed action, not the invalid requested skill.", "",
        "## Interpretation Boundaries", "",
        "This is a five-seed development pilot with a short fixed simulation horizon, one model, and fixed scene families. It cannot establish statistical superiority, deployment robustness, cross-model generality, or reward-guided adaptation. A horizon ending means only that the task was unfinished at the configured cutoff. The live pursuit executor already contains local interception heuristics shared by all groups, so outcomes describe the whole selection-and-execution path rather than learned low-level control.", "",
        "Role-template skill coverage is an engineering diagnostic, not independent ground truth. Activation is performed once per episode and may vary across calls even at temperature zero. Action availability can coincide across conditions when local preconditions eliminate irrelevant catalog entries; option counts and raw observations are retained for interpretation. No held-out conclusions or manuscript performance claims are added by this report.", "",
        f"Of the logged failures, {boundary_index_errors} returned an option index equal to the option count, outside the zero-based range. The reused local-agent prompt requests an integer option_index but does not explicitly state zero-based indexing or enumerate index labels. These observations are consistent with a zero-based/one-based convention mismatch, not proof of its internal cause. This interface ambiguity may affect choices even when the returned integer passes the range check. It must be resolved uniformly before a confirmatory effectiveness comparison; the current batch is not retroactively repaired or rerun.", "",
        "## Artifacts", "",
        "- `episode_metrics.csv`, `aggregate_metrics.csv`, `role_returns.csv`: descriptive metrics.",
        "- `activation_diagnostics.csv`: selected catalogs and engineering coverage diagnostics.",
        "- `paired_differences.csv`: same-seed differences, without a significance claim.",
        "- `analysis.json`, `decision_failures.csv`: request accounting and failure evidence.",
        "- `remote-artifacts/outputs/`: original plan, provider responses, complete traces and experience exports.",
        "- `remote-artifacts/inputs/`: frozen experiment code, catalog and preregistration.",
        "- `tests.log`, `preflight.log`, `batch.log`: execution logs.", ""]
    final_check_path = root / "final_memory_check.json"
    if final_check_path.exists():
        final_check = json.loads(final_check_path.read_text(encoding="utf-8"))
        lines += ["## Final Store Check", "", f"`{json.dumps(final_check, sort_keys=True)}`", ""]
    (root / "EXPERIMENT_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    hashes = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "artifact_hashes.json":
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "artifact_hashes.json").write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: analysis[key] for key in ("episodes", "records", "requests", "total_tokens", "usage_missing_requests", "api_errors", "verification")}))


if __name__ == "__main__":
    main()
