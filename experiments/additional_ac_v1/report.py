"""Audit downloaded measured records and produce descriptive paired summaries."""

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics


def moments(values):
    return dict(n=len(values), mean=statistics.mean(values) if values else None,
                sd=statistics.stdev(values) if len(values) > 1 else None)


def display(values):
    m = moments(values)
    if not m["n"]:
        return "not available"
    return f"{m['mean']:.2f} +/- {m['sd']:.2f}" if m["sd"] is not None else f"{m['mean']:.2f} (n=1; SD unavailable)"


def write_csv(path, rows):
    if rows:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.output
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    completion_path = root / "completion.json"
    completion = json.loads(completion_path.read_text(encoding="utf-8")) if completion_path.exists() else {"status": "running"}
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((root / "episodes").glob("*/summary.json"))]
    measured = [r for r in rows if r.get("status") in {"horizon", "complete"}]
    groups = defaultdict(list)
    for row in rows:
        groups[(row["experiment"], row["scenario"], row["seed"])].append(row)
    issues = []
    if completion.get("source_hashes_unchanged") is False:
        issues.append(dict(check="runtime_sources_changed_during_batch"))
    for key, group in groups.items():
        hashes = {r["initial_state_sha256"] for r in group if r.get("initial_state_sha256")}
        if len(hashes) > 1:
            issues.append(dict(check="paired_initial_states_differ", group=key))
    for row in rows:
        if row["episode_index"] == 1 and row.get("initial_memory_records") != 0:
            issues.append(dict(check="initial_memory_not_empty", episode=row["episode_id"]))
        if row["experiment"] == "C" and row.get("cross_episode_retrieval_references", 0):
            issues.append(dict(check="prompt_memory_leakage", episode=row["episode_id"]))
        if row["condition"] == "aeroweaver_no_rl" and row.get("memory_ranking_changes", 0):
            issues.append(dict(check="beta_zero_reranked", episode=row["episode_id"]))
        if row["experiment"] == "A" and row["episode_index"] > 1 and not row.get("initial_memory_records"):
            issues.append(dict(check="persistent_memory_missing", episode=row["episode_id"]))
    planned = [s for block in plan["schedule"] for s in block]
    seen = {r["episode_id"] for r in rows}
    pending = [s for s in planned if s["episode_id"] not in seen]
    usage = dict(calls=0, tokens=0, error_calls=0, response_models=set())
    provider_errors = []
    for path in (root / "calls").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            call = json.loads(line)
            usage["calls"] += 1
            usage["tokens"] += call.get("usage", {}).get("total_tokens", 0)
            usage["error_calls"] += bool(call.get("error"))
            if (call.get("error") or {}).get("type") == "HTTPError":
                provider_errors.append(dict(journal=path.name, episode_id=call.get("episode_id"),
                                            step=call.get("step"), error=call["error"]))
            if call.get("response", {}).get("model"):
                usage["response_models"].add(call["response"]["model"])
    usage["response_models"] = sorted(usage["response_models"])
    if len(usage["response_models"]) > 1:
        issues.append(dict(check="multiple_response_model_ids", models=usage["response_models"]))
    c_pairs, a_pairs, endpoints = [], [], []
    lookup = {(r["experiment"], r["scenario"], r["condition"], r["seed"], r["episode_index"]): r for r in measured}
    for task in plan["tasks"]:
        for seed in plan["seeds"]:
            base = lookup.get(("C", task, "original", seed, 1))
            for variant in ("paraphrased", "reordered", "distractors"):
                perturbed = lookup.get(("C", task, variant, seed, 1))
                if base and perturbed:
                    c_pairs.append(dict(scenario=task, seed=seed, perturbation=variant,
                                        original_return=base["controlled_team_mean_return"],
                                        perturbed_return=perturbed["controlled_team_mean_return"],
                                        delta=perturbed["controlled_team_mean_return"]-base["controlled_team_mean_return"]))
            for ep in range(1, 11):
                full = lookup.get(("A", task, "aeroweaver_pilot", seed, ep))
                ablated = lookup.get(("A", task, "aeroweaver_no_rl", seed, ep))
                if full and ablated:
                    a_pairs.append(dict(scenario=task, seed=seed, episode_index=ep,
                                        full_return=full["controlled_team_mean_return"],
                                        no_reward_return=ablated["controlled_team_mean_return"],
                                        delta=full["controlled_team_mean_return"]-ablated["controlled_team_mean_return"]))
            for condition in ("aeroweaver_pilot", "aeroweaver_no_rl"):
                start = lookup.get(("A", task, condition, seed, 1))
                end = lookup.get(("A", task, condition, seed, 10))
                if start and end:
                    endpoints.append(dict(scenario=task, seed=seed, condition=condition,
                                          first_return=start["controlled_team_mean_return"],
                                          last_return=end["controlled_team_mean_return"],
                                          delta=end["controlled_team_mean_return"]-start["controlled_team_mean_return"]))
    write_csv(root / "prompt_paired_differences.csv", c_pairs)
    write_csv(root / "adaptation_paired_differences.csv", a_pairs)
    write_csv(root / "adaptation_endpoint_changes.csv", endpoints)
    audit = dict(completion=completion, measured_episodes=len(measured), attempted_episodes=len(rows),
                 not_attempted=pending, issues=issues, usage=usage, provider_errors=provider_errors,
                 decision_errors=sum(r.get("decision_errors", 0) for r in rows),
                 invocation_errors=sum(r.get("invocation_errors", 0) for r in rows),
                 cross_episode_retrieval_references=sum(r.get("cross_episode_retrieval_references", 0) for r in rows))
    (root / "audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    lines = ["# Additional A/C Experiments: Measured Results", "",
             f"Status: {completion['status']}. Measured episodes: {len(measured)}/{len(planned)}; attempts: {len(rows)}.",
             f"Requested model: `{plan['model']}`. Observed response models: {', '.join(usage['response_models'])}.",
             "The requested and returned model identifiers are retained verbatim; their equivalence is not established by this experiment.",
             "", "## Protocol", "",
             "A: three independent environment-seed streams per task and condition, ten consecutive episodes per stream. "
             "The same layout is repeated within each stream; memory persists only within that stream. "
             "Full uses beta=0.8; w/o reward correction uses beta=0. All other settings are shared.",
             "C: four mission-text variants, three paired environment seeds, fresh isolated memory for each condition/seed. "
             "Task activation is shared and fixed; only the mission text at local action selection is perturbed. "
             "This is not an end-to-end perturbation of the skill activation stage.",
             "Each episode uses at most 24 rounds of the existing fixed-step Mock runtime. "
             "Reported returns are raw controlled-team means from the runtime reward functions, not native MPE rollouts. "
             "Mean +/- sample SD is over independent seeds, never over consecutive episodes. "
             "Only one text template is tested for each perturbation category. "
             "No significance or held-out generalization claim is made.",
             "", "## C: Prompt Sensitivity", "", "| Task | Condition | Return (mean +/- SD) | n |",
             "|---|---|---:|---:|"]
    for task in plan["tasks"]:
        for condition in ("original", "paraphrased", "reordered", "distractors"):
            values = [r["controlled_team_mean_return"] for r in measured if r["experiment"] == "C" and r["scenario"] == task and r["condition"] == condition]
            lines.append(f"| {task} | {condition} | {display(values)} | {len(values)} |")
    lines += ["", "## A: Adaptation Endpoints", "", "| Task | Condition | Episode 1 | Episode 10 | Paired change |", "|---|---|---:|---:|---:|"]
    for task in plan["tasks"]:
        for condition in ("aeroweaver_pilot", "aeroweaver_no_rl"):
            values = lambda ep: [r["controlled_team_mean_return"] for r in measured if r["experiment"] == "A" and r["scenario"] == task and r["condition"] == condition and r["episode_index"] == ep]
            changes = [r["delta"] for r in endpoints if r["scenario"] == task and r["condition"] == condition]
            lines.append(f"| {task} | {condition} | {display(values(1))} | {display(values(10))} | {display(changes)} |")
    if completion["status"] != "complete":
        lines += ["", "### Available A Episodes (Incomplete)", "",
                  "Do not interpret these partial trajectories as a ten-episode adaptation result. "
                  "At later indices the available seed subsets may differ between conditions.", "",
                  "| Task | Condition | Episode | Return (mean +/- SD) | n |", "|---|---|---:|---:|---:|"]
        a_groups = defaultdict(list)
        for row in measured:
            if row["experiment"] == "A":
                a_groups[(row["scenario"], row["condition"], row["episode_index"])].append(row["controlled_team_mean_return"])
        for (task, condition, ep), values in sorted(a_groups.items()):
            lines.append(f"| {task} | {condition} | {ep} | {display(values)} | {len(values)} |")
    if provider_errors:
        statuses = sorted({r["error"]["status"] for r in provider_errors})
        lines += ["", "## Provider Interruption", "",
                  f"The batch stopped after HTTP errors: {statuses}. Exact error bodies are retained in audit.json and request journals. "
                  "No automatic retry was attempted. Completed episodes are retained; interrupted episodes have no completed-return estimate."]
        if 402 in statuses:
            lines.append("The provider reported Insufficient Balance (HTTP 402). Funding or another explicitly authorized configuration is required before further paid calls. "
                         "Do not silently reuse interrupted-episode transitions as if they were completed training episodes when resuming.")
    lines += ["", "## Integrity And Scope", "",
              f"- Audit violations detected: {len(issues)}.",
              f"- Decision errors retained: {audit['decision_errors']}; invocation errors retained: {audit['invocation_errors']}.",
              f"- Cross-episode retrieval references: {audit['cross_episode_retrieval_references']}.",
              f"- Actual calls (including setup): {usage['calls']}; tokens: {usage['tokens']}; error calls: {usage['error_calls']}.",
              "- Technical failures have no completed return estimate; all attempts remain in the episode CSV and audit. No failed result is replaced by zero.",
              "- A shared empty memory and matched initial environment do not force identical first-episode returns: LLM sampling and within-episode correction can already differ.",
              "- Three seeds provide limited uncertainty estimation. Positive or negative changes are descriptive; the synthetic preview is not evidence.",
              "- Traces, full model request/response journals, reward manifests, source hashes, and isolated memory databases accompany the results.", ""]
    (root / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in audit.items() if k not in {"not_attempted", "completion"}}, indent=2))


if __name__ == "__main__":
    main()
