"""Audit all-task interventions and report every paired cell, without reruns."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sys

import analyze_pilot

LABELS = {
    "central_api": "Centralized API", "hmas2_adapted": "HMAS-2 adapted", "aeroweaver_pilot": "AeroWeaver",
    "aeroweaver_full_catalog": "Full catalog", "aeroweaver_no_peer": "No coordination reports",
    "aeroweaver_no_rl": "No reward correction",
}
TASKS_ZH = {"coverage": "协同覆盖", "pursuit": "追逐逃避", "navigation": "引导导航",
           "private_communication": "私有通信", "circle": "环形编队", "line": "线形编队",
           "world_communication": "异质信息协同", "collection": "采集投送", "concealment": "目标隐蔽"}
ABLATIONS = ("aeroweaver_full_catalog", "aeroweaver_no_peer", "aeroweaver_no_rl")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value):
    return "N/A" if value is None else f"{value:.3f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    # Reuse the existing reward, ranking and raw-call audit before checking interventions.
    sys.argv = [sys.argv[0], str(args.run)]
    analyze_pilot.main()
    output = args.run / "remote-artifacts/outputs"
    plan, completion = load(output / "plan.json"), load(output / "completion.json")
    rows = [load(path) for path in sorted((output / "episodes").glob("*/summary.json"))]
    by_cell = {(row["scenario"], row["method"]): row for row in rows}
    execution_counts = Counter("completed" if row["status"] in {"complete", "horizon"}
                               else "interrupted" if row["records"] else "blocked_before_first_step"
                               if row.get("calls", 0) else "not_started"
                               for row in rows)
    pending = [{"scenario": row["scenario"], "method": row["method"], "seed": row["seed"],
                "episode_id": row["episode_id"], "records": row["records"], "calls": row.get("calls", 0),
                "error": row.get("error"), "requires_episode_restart": bool(row["records"])}
               for row in rows if row["status"] not in {"complete", "horizon"}]
    pending_manifest = {"status": "finished" if not pending else "incomplete_requires_attention", "parent_run": args.run.name,
                        "parent_remote_output": load(args.run / "launch.json")["remote"] + "/outputs",
                        "retain_completed_episodes": execution_counts["completed"], "pending_episodes": pending,
                        "resume_constraints": ["Restore API balance before any new paid calls",
                            "Keep paired seed and archived shared activations",
                            "Do not rerun completed episodes or overwrite interrupted parent evidence",
                            "Explicit approval is required before restarting interrupted episodes",
                            "Global request concurrency at most 3"]}
    (args.run / "resume-manifest.json").write_text(json.dumps(pending_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    expected = {(task, method) for task in plan["scenarios"] for method in plan["methods"]}
    violations = list(load(args.run / "audit.json")["violations"])
    if set(by_cell) != expected or len(rows) != len(expected):
        violations.append("Missing or duplicate planned task/condition cell")
    if {row["seed"] for row in rows} != set(plan["seeds"]):
        violations.append("Unexpected seed")
    preserved_completed = 0
    archived_attempts_checked = 0
    if plan.get("parent_output"):
        parent = args.run.parent / Path(plan["parent_output"]).parent.name / "remote-artifacts/outputs"
        if parent.exists():
            parent_rows = [load(path) for path in (parent / "episodes").glob("*/summary.json")]
            current_ids = {row["episode_id"]: row for row in rows}
            for previous in parent_rows:
                if previous["status"] not in {"complete", "horizon"}:
                    continue
                episode = previous["episode_id"]
                current = current_ids.get(episode, {})
                fields = ("status", "records", "rounds", "controlled_team_mean_return", "returns_by_agent",
                          "task_success", "active_skills", "initial_state_sha256", "final_metrics")
                if any(previous.get(field) != current.get(field) for field in fields):
                    violations.append(f"Previously completed outcome changed: {episode}")
                for name in ("trace.jsonl", "experience.jsonl", "initial_state.json"):
                    original = parent / "episodes" / episode / name
                    retained = output / "episodes" / episode / name
                    if not retained.exists() or original.read_bytes() != retained.read_bytes():
                        violations.append(f"Previously completed evidence changed: {episode}/{name}")
                preserved_completed += 1
            if not (output / "calls.jsonl").read_bytes().startswith((parent / "calls.jsonl").read_bytes()):
                violations.append("Parent raw request journal was changed or removed")
            manifest = output / "restart_manifest.json"
            if manifest.exists():
                for attempt in load(manifest)["attempts"]:
                    episode = attempt["restarted_from"]
                    archive = output / "archived_attempts" / episode
                    for original in (parent / "episodes" / episode).rglob("*"):
                        if original.is_file():
                            retained = archive / original.relative_to(parent / "episodes" / episode)
                            if not retained.exists() or original.read_bytes() != retained.read_bytes():
                                violations.append(f"Interrupted evidence changed: {episode}/{original.name}")
                    archived_attempts_checked += 1
    catalog_names = {skill["name"] for skill in load(args.run / "remote-artifacts/inputs/catalog.json")}
    diagnostics = {}
    initial_groups = defaultdict(set)
    for row in rows:
        episode = row["episode_id"]
        directory = output / "episodes" / episode
        if row.get("initial_state_sha256"):
            initial_groups[row["scenario"]].add(row["initial_state_sha256"])
        if row["status"] == "technical_failure":
            diagnostics[episode] = {"error": row.get("error"), "status": row["status"]}
            continue
        fixed = set(plan["fixed_roles"][row["scenario"]])
        condition = row["method"]
        if condition in {"aeroweaver_pilot", "aeroweaver_no_peer", "aeroweaver_no_rl"}:
            expected_active = load(output / "shared_activations" / f"{row['scenario']}.json")["active"]
            if sorted(row["active_skills"]) != sorted(expected_active):
                violations.append(f"Unmatched shared activation: {episode}")
        elif set(row["active_skills"]) != catalog_names:
            violations.append(f"Incomplete full catalog: {episode}")
        events = [json.loads(line) for line in (directory / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        behavior = {"controlled_skills": Counter(), "message_kinds": Counter(),
                    "target_diversity_rounds": Counter(), "nonzero_advantage_decisions": 0,
                    "ranking_changes": 0, "final_metrics": row["final_metrics"]}
        for event in events:
            targets = []
            for rid, decision in event["decisions"].items():
                controlled = row["roles"][rid] not in fixed
                if controlled:
                    behavior["controlled_skills"][decision["choice"]["skill"]] += 1
                    if decision["choice"]["skill"] == "cover_landmark":
                        targets.append(decision["choice"]["parameters"]["target_id"])
                ranking = decision.get("ranking")
                if not ranking:
                    continue
                behavior["ranking_changes"] += ranking["changed_by_memory"]
                behavior["nonzero_advantage_decisions"] += any(abs(value) > 1e-9 for value in ranking["advantages"].values())
                expected_beta = 0 if condition == "aeroweaver_no_rl" else .8
                if ranking["beta"] != expected_beta:
                    violations.append(f"Incorrect beta: {episode}")
                if expected_beta == 0 and (ranking["changed_by_memory"] or ranking["selected"] != ranking["base_selected"]):
                    violations.append(f"No-RL changed the base selection: {episode}")
                if any(record["mission"] != episode for record in ranking["retrieved"]):
                    violations.append(f"Cross-episode retrieval in cold-start batch: {episode}")
            if targets:
                behavior["target_diversity_rounds"][len(set(targets))] += 1
            for message in event["messages"]:
                behavior["message_kinds"][message["kind"]] += 1
                if (condition == "aeroweaver_no_peer" and row["roles"][message["source"]] not in fixed
                        and message["kind"] in {"intent", "target"}):
                    violations.append(f"Coordination-message ablation leaked a report: {episode}")
        diagnostics[episode] = behavior
    if any(len(hashes) != 1 for hashes in initial_groups.values()):
        violations.append("Initial state pairing failed")

    local_calls_checked = 0
    for line in (output / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        if call["phase"] != "local_selector":
            continue
        data = json.loads(call["request"]["messages"][-1]["content"])
        state = data["local_state"]
        role = state["role"]
        allowed = {"private_goal": {"speaker", "informed"}, "private_key": {"sender", "receiver"}, "private_symbol": {"sender"}}
        for key, authorized in allowed.items():
            if key in state and role not in authorized:
                violations.append(f"Private field {key} exposed to {role}: {call['episode_id']}")
        if "private_state_for_pairing_only" in data:
            violations.append(f"Archived hidden state entered a prompt: {call['episode_id']}")
        local_calls_checked += 1

    pairs = []
    for task in plan["scenarios"]:
        full = by_cell.get((task, "aeroweaver_pilot"), {})
        for ablation in ABLATIONS:
            control = by_cell.get((task, ablation), {})
            a, b = full.get("controlled_team_mean_return"), control.get("controlled_team_mean_return")
            pairs.append({"scenario": task, "ablation": ablation, "full_return": a, "ablation_return": b,
                          "full_minus_ablation": a - b if a is not None and b is not None else None,
                          "full_model": full.get("model", plan["model"]), "ablation_model": control.get("model", plan["model"]),
                          "mixed_backbone": full.get("model", plan["model"]) != control.get("model", plan["model"]),
                          "full_status": full.get("status"), "ablation_status": control.get("status")})
    with (args.run / "ablation_differences.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(pairs[0]))
        writer.writeheader()
        writer.writerows(pairs)
    checks = {"verification_status": "ANALYZED", "planned_cells": len(expected), "observed_cells": len(rows),
              "execution_counts": dict(execution_counts),
              "rollout_models": dict(Counter(row.get("model", plan["model"]) for row in rows if row["status"] in {"complete", "horizon"})),
              "preserved_completed_episodes_checked": preserved_completed,
              "archived_interrupted_attempts_checked": archived_attempts_checked,
              "paired_task_groups": len(initial_groups), "local_prompt_permissions_checked": local_calls_checked,
              "violations": violations, "diagnostics": diagnostics}
    (args.run / "component_audit.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = load(args.run / "audit.json")
    lines = ["# 九任务主对比与组件消融", "", "## Material Passport",
        "- Origin Skill: academic-research-suite / experiment-agent。",
        "- Verification Status: ANALYZED；核验本批次记录，不是独立多种子复现实验。",
        f"- 批次：{args.run.name}；平台：192.0.2.10；配对种子：{plan['seeds']}。",
        f"- 计划 {len(expected)} 回合；实际完成 {execution_counts['completed']}，途中中断 {execution_counts['interrupted']}，首步前接口阻断 {execution_counts['blocked_before_first_step']}，未启动 {execution_counts['not_started']}。",
        "", "## 设置", "九任务分别对比两个 LLM 基线、完整 AeroWeaver 和三种单组件消融，每个条件执行一次。",
        "沿用 Mock 动力学及映射后的 MPE2 奖励函数，最多 24 个决策轮；每个条件从独立空经验库开始。",
        "完整方法、关闭协同消息和关闭奖励更新三组使用同一份任务激活结果。",
        "按用户要求保留原 V4 完成回合，续跑使用 deepseek-flash（用户指定 V4.1 Flash）；模型版本见逐回合表。不同模型的配对属于探索性比较，不假定基座等效。" if plan.get("mixed_models_authorized") else "同批次沿用相同模型配置。",
        "不改变上一轮覆盖策略，不补入其他方法或历史测试回合的经验。",
        "", "## 全部回报", "被测团队平均未折扣回合回报，同一任务内越高越好。",
        "", "| 任务 | Centralized API | HMAS-2 | AeroWeaver | 全技能目录 | 无协同消息 | 无奖励更新 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for task in plan["scenarios"]:
        values = [fmt(by_cell.get((task, method), {}).get("controlled_team_mean_return")) for method in LABELS]
        lines.append("| " + " | ".join([TASKS_ZH[task]] + values) + " |")
    lines += ["", "## 消融差值", "差值为完整 AeroWeaver 减去对应消融组；正值表示本回合完整方法回报更高。",
              "", "| 任务 | 减去全技能目录 | 减去无协同消息 | 减去无奖励更新 |",
              "| --- | ---: | ---: | ---: |"]
    for task in plan["scenarios"]:
        values = [fmt(pair["full_minus_ablation"]) for pair in pairs if pair["scenario"] == task]
        lines.append("| " + " | ".join([TASKS_ZH[task]] + values) + " |")
    lines += ["", "## 任务状态与开销",
        "私有通信的 complete 仅表示三轮通信协议结束，正确性查看最后两项任务指标。",
        "world communication 和 goal concealment 不定义二值成功率。",
        "accounted tokens 为该条件的实际调用，加上一份完整激活开销；共享激活只实际请求一次，不能将归属开销累加后当作账单总量。",
        "续跑批次使用全局 3 请求并发限制，继承回合使用原调度；不同调度阶段的延迟不能直接合并得出加速结论。",
        "", "| 任务 | 条件 | 模型 | 结束状态 | 轮数 | accounted tokens | 决策中位耗时 (s) | 重排序改变次数 | 最终任务指标 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for task in plan["scenarios"]:
        for method, label in LABELS.items():
            row = by_cell.get((task, method), {})
            metrics = json.dumps(row.get("final_metrics", {}), ensure_ascii=False, separators=(",", ":"))
            lines.append(f"| {TASKS_ZH[task]} | {label} | {row.get('model', plan['model'])} | {row.get('status', 'missing')} | {row.get('rounds', '')} | {row.get('accounted_tokens', '')} | {fmt(row.get('p50_decision_latency_s'))} | {row.get('memory_ranking_changes', '')} | `{metrics}` |")
    lines += ["", "## 核验与异常",
        f"- 实际模型请求 {completion['calls']} 次，实际报告 tokens {completion['tokens']:,}。",
        f"- 保存 {audit['recorded_transitions']} 条轨迹，核验 {audit['discount_values_checked']} 个折扣回报。",
        f"- 核验 {audit['retrieved_records_checked']} 条检索引用；跨回合引用 {audit['cross_episode_retrievals']}。",
        f"- 完整性及消融开关异常：{len(violations)}。",
        f"- 核验原样保留的完成回合 {preserved_completed} 个；核验中断尝试归档 {archived_attempts_checked} 个。",
        f"- 经验导出异常 {completion.get('artifact_export_failures', 0)} 个；如存在，其汇总由已完成轨迹离线恢复，原始错误与恢复清单另存，未追加实验。",
        f"- 接口请求错误：{audit['usage'].get('call_errors', 0)}；不完整输出：{len(audit['truncated_calls'])}。",
        "- 最终非法动作：" + json.dumps(audit["invalid_action_reasons"], ensure_ascii=False) + "。",
        "- 决策错误：" + json.dumps(audit["decision_error_types"], ensure_ascii=False) + "。",
        "- 局部反馈错误：" + json.dumps(audit["review_error_types"], ensure_ascii=False) + "。",
        "", "## 解释范围",
        "- 每格只有一个回合；差值是单种子描述性结果，不计算显著性或跨任务原始奖励总分。",
        "- 无协同消息组只屏蔽受控 agent 的 intent/target 报告，保留导航 goal 和私有通信 ciphertext；不能将其称为关闭全部通信。",
        "- 奖励更新组从空记忆开始，检验的是回合内校正，不是跨任务持续学习。",
        "- 环形与线形编队的槽位由环境分配，结果不等于模型自行设计或分配队形。",
        "- baseline 与完整方法间有多项差异，中央基线对比不能独立证明分布式决策收益。",
        "- MAPPO/MADDPG 不在本轮 LLM 实验中，没有借用其他环境的数据。",
        "", "## 资料", "原始记录见 remote-artifacts/outputs；主指标见 episode_metrics.csv；配对差值见 ablation_differences.csv；逐回合行为与开关检查见 component_audit.json。"]
    for violation in violations:
        lines.append("- 核验问题：" + violation)
    (args.run / "ALL_TASKS_REPORT.zh-CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    hashes = {str(path.relative_to(args.run)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in args.run.rglob("*") if path.is_file() and path.name != "artifact_hashes.json"}
    (args.run / "artifact_hashes.json").write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cells": len(rows), "violations": violations, "report": str(args.run / "ALL_TASKS_REPORT.zh-CN.md")}, ensure_ascii=False))
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
