"""Recompute recorded pilot metrics and preserve a labeled post-hoc sensitivity check."""

import argparse
from collections import defaultdict
from copy import deepcopy
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    root = args.run
    inputs = root / "inputs" / "experiments" / "skill_activation_v1"
    manifest = json.loads((root / "run_manifest.json").read_text())
    for name, expected in manifest["input_hashes"].items():
        assert hashlib.sha256((inputs / name).read_bytes()).hexdigest() == expected
    labels = json.loads((inputs / "labels.json").read_text())["requirements"]
    raw = [json.loads(line) for line in (root / "raw_rankings.jsonl").read_text().splitlines()]
    by_id = {r["query_id"]: r for r in raw}
    assert len(by_id) == len(raw) == 36
    assert all(r["status"] == "ok" for r in raw)
    with (root / "metrics.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 432
    assert len({(r["query_id"], r["method"], r["k"]) for r in rows}) == 432
    for row in rows:
        selection = set(json.loads(row["selected"]))
        if row["method"] == "semantic_topk":
            assert selection == set(by_id[row["query_id"]]["ranking"][:int(row["k"])])
        label = labels[row["scenario_id"] + "/" + row["role"]]
        hits = [not selection.isdisjoint(group) for group in label["required_groups"]]
        assert math.isclose(float(row["required_coverage"]), sum(hits) / len(hits))
        assert int(row["complete_coverage"]) == int(all(hits))
        precision = len(selection.intersection(label["useful"])) / len(selection) if selection else 0
        assert math.isclose(float(row["useful_precision"]), precision)

    # This is a post-hoc interpretation, not a rewrite of the preregistered labels.
    equivalent = deepcopy(labels)
    equivalent["world_communication/leader"]["required_groups"] = [
        ["pursue_target"], ["signal_target", "pursue_target"],
    ]
    sensitivity = {}
    for method in {r["method"] for r in rows}:
        subset = [r for r in rows if r["method"] == method and r["k"] == "3"]
        scenarios = defaultdict(list)
        correct = 0
        for row in subset:
            selected = set(json.loads(row["selected"]))
            label = equivalent[row["scenario_id"] + "/" + row["role"]]
            hits = [bool(selected.intersection(group)) for group in label["required_groups"]]
            correct += all(hits)
            scenarios[row["scenario_id"]].append(sum(hits) / len(hits))
        sensitivity[method] = {"complete": correct, "queries": len(subset),
                               "macro_required_coverage": statistics.mean(statistics.mean(v) for v in scenarios.values())}
    usage = {key: sum(r["response"]["usage"][key] for r in raw)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    summary = json.loads((root / "summary.json").read_text())
    assert usage == summary["actual_semantic_usage"]
    audit = {"status": "ANALYZED", "raw_queries": 36, "metric_rows_verified": 432,
             "frozen_input_hashes_verified": True, "original_scores_reproduced": True,
             "model_response_ids": sorted({r["response"].get("response_model") for r in raw}),
             "actual_provider_usage": usage, "posthoc_equivalence": sensitivity,
             "posthoc_reason": "Live sim/mock_tasks.py:556-557 sends a target report during pursue_target; a separate signal_target is not mandatory for that capability.",
             "labels_independently_reviewed": False, "model_inference_rerun": False}
    (root / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    lines = ["# Pilot Validation", "", "## Material Passport", "",
             "- Origin Mode: validate", "- Verification Status: ANALYZED",
             "- Coverage: 432 metric rows recomputed from frozen labels; 36 raw model rankings checked.",
             "- Model inference was not rerun; this is arithmetic/provenance verification only.", "",
             "## Annotation Sensitivity", "",
             "Frozen labels required signal_target separately for the mobile leader. The deployed pursue_target executor already sends a target message (sim/mock_tasks.py:556-557).",
             "The original labels, raw outputs, and primary report are unchanged. The following post-hoc check permits either skill to fulfill the reporting capability, while still requiring pursuit.", "",
             "| Method | Complete coverage at K=3 after equivalence check |", "| --- | ---: |"]
    for name, value in sorted(sensitivity.items()):
        lines.append(f"| {name} | {value['complete']}/{value['queries']} |")
    lines += ["", "The lexical method changes from 34/36 to 35/36. Its remaining miss is world-02/pursuer, which selected food, evasion, and cover instead of pursuit.",
              "This check demonstrates label sensitivity and must not be presented as a newly preregistered primary score.", "",
              "## Statistical Fallacy Scan (11/11)", "",
              "| Check | Finding |", "| --- | --- |",
              "| Simpson reversal | Scenario tables retained; only descriptive macro averages reported. |",
              "| Ecological inference | Role-query results do not establish episode or swarm success. |",
              "| Selection bias | Small author-generated corpus; population generalization is unsupported. |",
              "| Collider conditioning | No outcome-conditioned inclusion; all scheduled queries retained. |",
              "| Base rates | Required-capability prevalence and full-catalog reference shown; precision interpretation depends on provisional useful labels. |",
              "| Regression to mean | No selected-extreme pre/post comparison or improvement claim. |",
              "| Survivorship | 36/36 calls included, zero failures; no filtering of difficult cases. |",
              "| Multiple searching | All three prespecified K settings retained; no significance tests. |",
              "| Researcher choices | Inputs frozen before calls; post-hoc label equivalence explicitly separated. Independent labels still needed. |",
              "| Causal overreach | No claim about execution success or total inference cost from retrieval metrics. |",
              "| Reverse causality | Labels were frozen before outcomes; no inference about deployment learning. |", ""]
    (root / "validation.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
