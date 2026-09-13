"""Render source-grounded pilot results; missing outcomes are never plotted as zero."""

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import MaxNLocator

TASKS = ["coverage", "pursuit", "navigation", "private_communication", "circle", "line",
         "world_communication", "collection", "concealment"]
TASK_LABELS = ["Coverage", "Pursuit-evasion", "Guided navigation", "Private communication", "Circular formation",
               "Line formation", "World communication", "Collection-delivery", "Goal concealment"]
METHODS = ["central_api", "hmas2_adapted", "aeroweaver_pilot", "aeroweaver_full_catalog",
           "aeroweaver_no_peer", "aeroweaver_no_rl"]
HEADERS = ["Centralized\nAPI", "HMAS-2\nadapted", "AeroWeaver", "Full\ncatalog", "No coordination\nreports", "No reward\ncorrection"]
SHORT = ["API", "HMAS-2", "AeroWeaver", "All skills", "No reports", "No RL"]
COLORS = ["#526F8A", "#919FAD", "#087F8C", "#879889", "#AC9273", "#8C84A5"]
INK, MUTED, GRID, TEAL = "#24333B", "#64717B", "#DCE2E5", "#087F8C"
WARN, WARN_BG = "#9B5824", "#FFF2E3"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def number(value):
    return None if value is None or value == "" else float(value)


def state(row):
    if row["status"] in {"complete", "horizon"}:
        return "completed"
    if number(row.get("records")):
        return "interrupted"
    return "blocked" if number(row.get("calls")) else "pending"


def has_error(row):
    return any(number(row.get(key)) for key in ("decision_errors", "invocation_errors", "review_errors"))


def legacy_model(row):
    return row.get("model") == "deepseek-v4-flash"


def initialize_style():
    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Arial", "DejaVu Sans"],
        "font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
        "axes.linewidth": .7, "axes.edgecolor": GRID, "text.color": INK,
        "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": INK,
        "svg.fonttype": "none", "pdf.fonttype": 42, "axes.unicode_minus": False,
        "legend.frameon": False, "savefig.facecolor": "white"})


def heading(fig, title, subtitle, status):
    fig.text(.045, .953, title, fontsize=19, fontweight="bold", va="top")
    fig.text(.045, .905, subtitle, fontsize=10, color=MUTED, va="top")
    fig.text(.955, .945, status, fontsize=11, fontweight="bold", color=WARN, ha="right", va="top")


def return_matrix(cells, count, seed):
    fig = plt.figure(figsize=(11.2, 7.1))
    heading(fig, "Nine-task comparison", f"Single-episode pilot | Seed {seed} | Raw team return; higher is better within each task", f"{count} / 54 completed")
    ax = fig.add_axes([.04, .19, .92, .66])
    ax.axis("off")
    values = []
    for task, label in zip(TASKS, TASK_LABELS):
        entries = []
        for method in METHODS:
            row = cells[task, method]
            status = state(row)
            value = number(row.get("controlled_team_mean_return"))
            entries.append(f"{value:.2f}" + (" *" if has_error(row) else "") + (" [4]" if legacy_model(row) else "") if status == "completed"
                           else {"pending": "--", "blocked": "B", "interrupted": "I"}[status])
        values.append([label] + entries)
    table = ax.table(cellText=values, colLabels=["Task"] + HEADERS, cellLoc="center",
                     colWidths=[.235, .12, .12, .12, .12, .1425, .1425], bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(10.2)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_linewidth(.55)
        cell.PAD = .09
        if r == 0:
            cell.set_facecolor("#EAF1F3" if c == 3 else "#F2F5F6")
            cell.set_text_props(fontweight="bold", color=TEAL if c == 3 else INK, fontsize=10)
            cell.set_height(cell.get_height() * 1.4)
        elif c == 0:
            cell.set_text_props(ha="left", fontsize=10)
            cell.set_facecolor("white")
        else:
            row = cells[TASKS[r - 1], METHODS[c - 1]]
            if state(row) == "completed":
                cell.set_facecolor(WARN_BG if has_error(row) else "#EAF5F4" if c == 3 else "white")
                cell.set_text_props(color=WARN if has_error(row) else TEAL if c == 3 else INK,
                                    fontweight="bold" if c == 3 else "normal")
            else:
                cell.set_facecolor("#F6F7F8")
                cell.set_text_props(color="#9AA3A8")
    fig.text(.045, .14, "All planned task conditions have completed rollouts; task success is reported separately." if count == 54 else "--  Not started     I  Interrupted rollout     B  Provider blocked before the first transition", fontsize=10, color=MUTED)
    fig.text(.045, .105, "*  Recorded decision, execution or local-review errors; affected results are retained.", fontsize=10, color=WARN)
    fig.text(.045, .065, "One episode per cell. Missing cells are not zero. This is an exploratory pilot, not a multi-seed benchmark.", fontsize=9, color=MUTED)
    if any(legacy_model(row) for row in cells.values()):
        fig.text(.045, .030, "[4] Retained V4 Flash; unmarked new results use deepseek-flash (V4.1 Flash). Model differences are not controlled.", fontsize=9, color=MUTED)
    return fig


def ablation_plot(cells, pairs, seed):
    fig, axes = plt.subplots(1, 3, figsize=(11.2, 6.7), sharey=True)
    available = sum(pair["difference"] is not None for pair in pairs)
    heading(fig, "Component ablations", f"Full AeroWeaver minus the ablated condition | Paired initial seed {seed}", f"{available} / 27 pairs available")
    fig.subplots_adjust(left=.20, right=.94, top=.80, bottom=.22, wspace=.16)
    bound = max([abs(pair["difference"]) for pair in pairs if pair["difference"] is not None] + [1]) * 1.5
    for j, (ax, method) in enumerate(zip(axes, METHODS[3:])):
        ax.set_title(["Skill activation", "Coordination reports", "Reward correction"][j], fontsize=11, pad=18, fontweight="bold")
        ax.axvline(0, color="#A9B2B8", lw=.9, linestyle=(0, (3, 3)), zorder=0)
        ax.set_xlim(-bound, bound)
        ax.set_ylim(8.6, -.6)
        ax.set_yticks(range(9), TASK_LABELS)
        ax.tick_params(axis="y", length=0, labelsize=9.5, pad=8)
        ax.tick_params(axis="x", length=3, labelsize=9)
        ax.spines["left"].set_visible(False)
        ax.grid(axis="y", color="#ECF0F2", linewidth=.7)
        ax.set_xlabel("Return difference", fontsize=10, labelpad=9)
        for i, task in enumerate(TASKS):
            pair = next(p for p in pairs if p["scenario"] == task and p["ablation"] == method)
            value = pair["difference"]
            if value is None:
                ax.text(bound * .49, i, "--", va="center", ha="center", color="#BAC1C5", fontsize=11)
            else:
                color = TEAL if value >= 0 else "#8D5878"
                ax.hlines(i, 0, value, color=color, linewidth=2.8)
                ax.scatter(value, i, s=52, facecolors="white" if pair["mixed_backbone"] else color,
                           edgecolors=color, linewidths=1.4, zorder=3)
                ax.annotate(f"{value:+.2f}" + (" *" if pair["error_flag"] else ""), (value, i),
                            xytext=(6 if value >= 0 else -6, 0), textcoords="offset points",
                            ha="left" if value >= 0 else "right", va="center", fontsize=10, fontweight="bold", color=color)
    fig.text(.045, .13, "Positive differences favor the full method. Raw reward scales differ across tasks; do not average these differences.", fontsize=9.5, color=MUTED)
    fig.text(.045, .09, "No reports: remove intent / target reports, but retain task-required goal / ciphertext channels. No RL: beta = 0.", fontsize=9.2, color=MUTED)
    fig.text(.045, .05, "All 27 pairs are available. Single paired episodes do not establish statistical significance." if available == 27 else "Unpaired conditions are left unavailable. A single paired episode does not establish statistical significance.", fontsize=9.2, color=MUTED)
    if any(pair["mixed_backbone"] for pair in pairs):
        fig.text(.045, .017, "Hollow points: different rollout backbones. These differences do not isolate the ablated component alone.", fontsize=9.2, color=MUTED)
    return fig


def cost_plot(cells, count, seed):
    fig, axes = plt.subplots(3, 3, figsize=(11.2, 8.2))
    heading(fig, "Inference cost by task", f"Accounted tokens per completed episode | Single seed {seed} | No missing-value imputation", f"{count} completed rollouts")
    fig.subplots_adjust(left=.13, right=.955, top=.815, bottom=.16, wspace=.48, hspace=.52)
    max_cost = max(number(row["accounted_tokens"]) for row in cells.values() if state(row) == "completed") / 1000
    for index, (ax, task, title) in enumerate(zip(axes.flat, TASKS, TASK_LABELS)):
        ax.set_title(f"{chr(97 + index)}  {title}", loc="left", fontsize=10.5, pad=8, fontweight="bold")
        ax.set_xlim(0, max_cost * 1.48)
        ax.set_ylim(5.6, -.6)
        ax.set_yticks(range(6), SHORT, fontsize=8.5)
        ax.tick_params(axis="y", length=0, pad=5)
        ax.tick_params(axis="x", length=3, labelsize=8)
        ax.xaxis.set_major_locator(MaxNLocator(3, min_n_ticks=3))
        ax.set_xlabel("Tokens (thousands)", fontsize=8.5, labelpad=3)
        ax.spines["left"].set_visible(False)
        ax.grid(axis="x", color="#E9EDF0", linewidth=.65, zorder=0)
        for j, method in enumerate(METHODS):
            row = cells[task, method]
            if state(row) != "completed":
                ax.text(max_cost * .025, j, "not complete", fontsize=7.5, color="#B0B9BE", va="center")
                continue
            cost = number(row["accounted_tokens"]) / 1000
            ax.barh(j, cost, height=.53, color=COLORS[j], zorder=2)
            ax.text(cost + max_cost * .025, j, f"{cost:.1f}" + (" *" if has_error(row) else "") + (" [4]" if legacy_model(row) else ""),
                    va="center", fontsize=8, color=WARN if has_error(row) else INK)
    fig.text(.045, .085, "Each activation-using condition is charged one full setup for comparison; shared activation is billed only once per task.", fontsize=9, color=MUTED)
    fig.text(.045, .055, "Episode lengths vary with termination. * Output / review errors retained. Latencies are not pooled across scheduling regimes.", fontsize=9, color=MUTED)
    if any(legacy_model(row) for row in cells.values()):
        fig.text(.045, .026, "[4] Retained V4 Flash; other completed results use deepseek-flash. Token counts are not monetary costs.", fontsize=9, color=MUTED)
    return fig


def write_csv(path, records):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    source = args.run / "remote-artifacts/outputs/episode_metrics.csv"
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    cells = {(row["scenario"], row["method"]): row for row in rows}
    assert len(rows) == len(cells) == 54
    assert set(cells) == {(task, method) for task in TASKS for method in METHODS}
    seeds = {row["seed"] for row in rows}
    assert len(seeds) == 1
    seed = next(iter(seeds))
    for row in rows:
        if state(row) == "completed":
            assert number(row.get("controlled_team_mean_return")) is not None
        else:
            assert number(row.get("controlled_team_mean_return")) is None
    counts = Counter(state(row) for row in rows)
    pairs = []
    for task in TASKS:
        full = cells[task, "aeroweaver_pilot"]
        for method in METHODS[3:]:
            control = cells[task, method]
            available = state(full) == state(control) == "completed"
            a = number(full["controlled_team_mean_return"]) if available else None
            b = number(control["controlled_team_mean_return"]) if available else None
            pairs.append({"scenario": task, "ablation": method, "full_return": a, "ablated_return": b,
                          "difference": a - b if available else None,
                          "full_model": full.get("model", "unrecorded"), "ablated_model": control.get("model", "unrecorded"),
                          "mixed_backbone": available and full.get("model") != control.get("model"),
                          "error_flag": has_error(full) or has_error(control) if available else False})
    target = args.run / "visual-summary"
    target.mkdir(exist_ok=True)
    data = [{**row, "execution_state": state(row), "error_flag": has_error(row)} for row in rows]
    write_csv(target / "source_data.csv", data)
    write_csv(target / "ablation_data.csv", pairs)
    matrix = []
    for task in TASKS:
        row = {"task": task}
        row.update({method: number(cells[task, method]["controlled_team_mean_return"]) for method in METHODS})
        matrix.append(row)
    write_csv(target / "return_matrix.csv", matrix)
    initialize_style()
    figures = [("01_task_returns", return_matrix(cells, counts["completed"], seed)),
               ("02_ablation_differences", ablation_plot(cells, pairs, seed)),
               ("03_inference_cost", cost_plot(cells, counts["completed"], seed))]
    qa = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "cells": len(cells),
          "execution_counts": dict(counts), "paired_ablations": sum(p["difference"] is not None for p in pairs),
          "complete_results_with_errors": sum(has_error(row) for row in rows if state(row) == "completed"),
          "missing_numeric_values": sum(number(row["controlled_team_mean_return"]) is None for row in rows),
          "backend": "matplotlib", "seed": seed,
          "rollout_models": dict(Counter(row.get("model", "unrecorded") for row in rows if state(row) == "completed")),
          "mixed_backbone_pairs": sum(p["mixed_backbone"] for p in pairs), "figures": {}}
    with PdfPages(target / "experiment_summary.pdf") as pdf:
        for name, fig in figures:
            fig.savefig(target / f"{name}.png", dpi=300)
            fig.savefig(target / f"{name}.pdf")
            fig.savefig(target / f"{name}.svg")
            pdf.savefig(fig)
            pixels = plt.imread(target / f"{name}.png")[..., :3]
            assert float(pixels.std()) > .03, "Blank raster output"
            svg = (target / f"{name}.svg").read_text(encoding="utf-8")
            assert "<text" in svg, "SVG text is not editable"
            qa["figures"][name] = {"pixel_shape": list(pixels.shape), "pixel_std": float(pixels.std()), "editable_svg_text": True}
            plt.close(fig)
    (target / "figure_qa.json").write_text(json.dumps(qa, indent=2) + "\n", encoding="utf-8")
    captions = """# Figure Captions

1. **Nine-task pilot results.** Mean undiscounted episode return over the controlled team, with one initial seed per task and condition. All completed rollouts are retained. Asterisks identify recorded decision, execution or local-review errors. Missing, interrupted and provider-blocked conditions are shown separately, not as zero-valued outcomes. Raw returns are comparable within tasks, not across tasks.
2. **Paired component ablations.** Full AeroWeaver minus each matched ablation, reported only when both episodes completed. All matched conditions use the same task seed; activation-using conditions share the archived activation result. The coordination ablation suppresses intent and target reports while preserving task-required goal and ciphertext channels. The reward ablation sets beta to zero. No uncertainty or significance is estimated from these single episodes.
3. **Inference token cost.** Tokens for completed episodes, including a full activation setup attributed to each condition that uses it. Actual shared activation is requested once per task, so attributed costs must not be summed to infer total billed usage. Episode lengths depend on termination. Flagged rollout errors are retained. Latency across distinct concurrency regimes is not compared.

Where indicated, [4] identifies retained V4 Flash rollouts; new rollouts use deepseek-flash, identified by the user as V4.1 Flash. Hollow ablation markers indicate mixed-backbone pairs. These exploratory differences are not fixed-backbone component effects. Archived task activations are reused unchanged across the continuation.
"""
    if any(row.get("artifact_export_error") for row in rows):
        captions += "\nThe collection full-catalog summary was recovered offline from its finalized 96-record SQLite buffer and complete 24-step trace after JSONL export failed; no rollout was repeated. Original exports and the recovery manifest are retained.\n"
    (target / "CAPTIONS.md").write_text(captions, encoding="utf-8")
    balance_path = args.run / "balance-check-latest.json"
    if not balance_path.exists():
        balance_path = args.run / "balance-check.json"
    balance = read_json(balance_path) if balance_path.exists() else None
    report = ["# 实验图表阶段汇总", "", "## 当前状态",
              f"共 54 个任务条件，已完成 {counts['completed']}，途中中断 {counts['interrupted']}，首步前阻断 {counts['blocked']}，未启动 {counts['pending']}。",
              "单回合探索性实验汇总；未完成条件保持缺失。图表可随新增结果用同一脚本更新。",
              "", "## 图表", "- 01_task_returns：九任务主对比表，保留全部已完成结果并明确缺失状态。",
              "- 02_ablation_differences：完整方法减去消融组，仅计算已经配齐的回合。",
              "- 03_inference_cost：按任务展示已完成回合的 token 开销，包含单次激活归属成本。",
              "", "## 阅读提示", "星号表示该回合存在输出或局部反馈错误，结果仍保留。",
              "原始回报不能跨任务直接求平均；单回合不能支持显著性结论。",
              f"已有 {sum(p['difference'] is not None for p in pairs)}/27 组配对差值，见 ablation_data.csv。",
              f"其中 {sum(p['mixed_backbone'] for p in pairs)} 组使用不同模型基座，以空心点标出；保留结果不代表基座差异可忽略。",
              "", "## 可用文件", "experiment_summary.pdf 为三页图表合集；每张图另有 PNG、PDF、SVG。",
              "source_data.csv 包含全部 54 个条件及执行状态；return_matrix.csv 和 ablation_data.csv 分别为回报矩阵和配对差值。",
              "CAPTIONS.md 提供英文图注；figure_qa.json 保存数据数量和图像核验结果。"]
    if balance:
        report.extend(["", "## 余额检查", f"查询时间：{balance['checked_at']}；接口可用：{balance['balance']['is_available']}。",
                       "该余额来自独立只读查询；实验调用开销见运行记录。"])
    (target / "SUMMARY.zh-CN.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "qa": qa}, indent=2))


if __name__ == "__main__":
    main()
