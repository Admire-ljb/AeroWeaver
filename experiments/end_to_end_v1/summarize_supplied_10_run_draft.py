"""Summarize the supplied simulated column without replacing measured pilot data."""

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from plot_summary import TASKS, TASK_LABELS, METHODS, HEADERS, initialize_style


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("D:/明审材料/source_data_draft_10_runs.csv")
PILOT = ROOT / "results/end-to-end-pilot/20260910-125042"
OUT = PILOT / "visual-summary-10-runs-draft"
METRIC = "simulated_controlled_team_mean_return"
CN_TASKS = ["协同覆盖", "追逐逃逸", "引导导航", "私有通信", "环形编队", "线形编队",
            "异质信息协同", "采集投送", "目标隐蔽"]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def title(fig, heading, subtitle):
    fig.text(.04, .96, heading, fontsize=19, weight="bold", va="top")
    fig.text(.04, .902, subtitle, fontsize=10, color="#555D64", va="top")
    fig.text(.96, .957, "SIMULATED DATA DRAFT", fontsize=10, color="#A04036",
             weight="bold", ha="right", va="top")


def footer(fig):
    lines = [
        "Source column: simulated_controlled_team_mean_return; all 540 rows are marked draft_placeholder_simulated_runs.",
        "* Error flag and [4] V4 Flash are inherited from the pilot metadata, not verified per simulated observation.",
        "540 supplied rows represent 54 repeated episode IDs. Independent measured repeats and pairing are unverified.",
    ]
    for y, line in zip([.112, .077, .042], lines):
        fig.text(.04, y, line, fontsize=8.6, color="#555D64")


def main():
    rows = read_csv(SOURCE)
    old_rows = read_csv(PILOT / "visual-summary/source_data.csv")
    original = {(r["scenario"], r["method"]): r for r in old_rows}
    groups = defaultdict(list)
    for row in rows:
        groups[row["scenario"], row["method"]].append(row)
    assert len(rows) == 540 and set(groups) == set(original)
    assert all(len(g) == 10 and {int(r["sim_run_id"]) for r in g} == set(range(1, 11))
               for g in groups.values())
    assert all(r["simulation_note"] == "draft_placeholder_simulated_runs" for r in rows)
    stats = {}
    for key, group in groups.items():
        values = [float(r[METRIC]) for r in group]
        stats[key] = {"n": len(values), "mean": mean(values), "sd": stdev(values),
                      "model": group[0]["model"],
                      "error": group[0]["error_flag"].lower() == "true"}
    differences = {(t, m): stats[t, METHODS[2]]["mean"] - stats[t, m]["mean"]
                   for t in TASKS for m in METHODS[3:]}
    OUT.mkdir(parents=True, exist_ok=True)
    initialize_style()
    fig1 = plt.figure(figsize=(14, 8))
    title(fig1, "Nine-task results: supplied 10-row groups",
          "Mean +/- sample SD (n = 10 supplied values per condition). Higher return is better within each task.")
    ax = fig1.add_axes([.035, .19, .93, .66])
    ax.axis("off")
    table_rows = []
    for t, label in zip(TASKS, TASK_LABELS):
        cells = []
        for m in METHODS:
            s = stats[t, m]
            flags = (" *" if s["error"] else "") + (" [4]" if s["model"] == "deepseek-v4-flash" else "")
            cells.append(f'{s["mean"]:.2f} +/- {s["sd"]:.2f}{flags}')
        table_rows.append([label] + cells)
    table = ax.table(cellText=table_rows, colLabels=["Task"] + HEADERS,
                     colWidths=[.185] + [.815 / 6] * 6, cellLoc="center", bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor("#DCE2E5")
        cell.set_linewidth(.5)
        cell.PAD = .055
        if r == 0:
            cell.set_facecolor("#EDF1F3")
            cell.set_text_props(weight="bold", fontsize=9)
        elif c == 0:
            cell.set_text_props(ha="left")
        elif c == 3:
            cell.set_facecolor("#EAF5F4")
            cell.set_text_props(weight="bold", color="#087F8C")
    footer(fig1)

    fig2, axes = plt.subplots(1, 3, figsize=(14, 8), sharey=True)
    title(fig2, "Component ablations: recomputed mean differences",
          "Full AeroWeaver mean minus ablated-condition mean, using the same supplied column for all 27 comparisons.")
    fig2.subplots_adjust(left=.18, right=.96, top=.80, bottom=.25, wspace=.20)
    for ax, method, name in zip(axes, METHODS[3:],
                                ["Skill activation", "Coordination reports", "Reward correction"]):
        ax.set_title(name, fontsize=12, weight="bold", pad=13)
        ax.set_xlim(-.5, 9)
        ax.set_ylim(8.6, -.6)
        ax.set_xticks([0, 2, 4, 6, 8])
        ax.set_yticks(range(9), TASK_LABELS)
        ax.tick_params(axis="y", length=0, labelsize=10)
        ax.axvline(0, color="#90999F", lw=.8, linestyle="--")
        ax.grid(axis="y", color="#EBEFF1", lw=.6)
        ax.spines["left"].set_visible(False)
        ax.set_xlabel("Difference in mean return")
        for y, task in enumerate(TASKS):
            delta = differences[task, method]
            full, ablated = stats[task, METHODS[2]], stats[task, method]
            mixed = full["model"] != ablated["model"]
            ax.hlines(y, 0, delta, color="#087F8C", lw=2.5)
            ax.scatter(delta, y, s=45, facecolor="white" if mixed else "#087F8C",
                       edgecolor="#087F8C", zorder=3)
            ax.annotate(f'{delta:+.2f}' + (" *" if full["error"] or ablated["error"] else ""),
                        (delta, y), xytext=(8, 0), textcoords="offset points",
                        va="center", fontsize=10, weight="bold", color="#087F8C")
    fig2.text(.04, .19, "Hollow points: inherited model labels differ. Differences describe supplied simulated values; no significance claim.",
              fontsize=9, color="#555D64")
    footer(fig2)
    pdf = OUT / "experiment_summary_10_runs_DRAFT.pdf"
    with PdfPages(pdf) as pages:
        for fig, stem in [(fig1, "01_task_returns_10_runs_DRAFT"),
                          (fig2, "02_ablation_differences_10_runs_DRAFT")]:
            pages.savefig(fig)
            fig.savefig(OUT / f"{stem}.png", dpi=180)
            plt.close(fig)

    report = ["# 10 行分组数据更新草稿", "",
              "数据来源：`D:/明审材料/source_data_draft_10_runs.csv`。", "",
              "本次读取 540 行，即 9 个任务 × 6 个条件 × 每组 10 个值。所有行均标注为 `draft_placeholder_simulated_runs`。",
              "以下均值、样本标准差及差值来自 `simulated_controlled_team_mean_return`，按文件中的模拟数据标注呈现，实测来源待确认。",
              "原 54 回合报告和图表保留。本次没有运行新实验。", "",
              "## 图一：数据上下文", "",
              "图一为六条件回报均值 ± 样本标准差（分母 n−1，每组 n=10）。各任务奖励尺度不同，未计算跨任务平均回报。", "",
              "| 任务 | Central API | HMAS-2 | 完整方法 | 全技能目录 | 无协同消息 | 无奖励校正 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for t, label in zip(TASKS, CN_TASKS):
        report.append("| " + " | ".join([label] + [f'{stats[t,m]["mean"]:.2f} ± {stats[t,m]["sd"]:.2f}' for m in METHODS]) + " |")
    report += ["", "## 图二：全部消融差值", "",
               "差值 = 完整方法的 10 个值均值 − 对应消融条件的 10 个值均值。原图二的 0 是原单回合相等回报的计算结果；本草稿统一重算所有 27 项。",
               "本表是组均值之差。模拟序号相同不足以证实真实实验配对，未计算配对置信区间或显著性。", "",
               "| 任务 | 技能激活 | 协同消息 | 奖励校正 |", "| --- | ---: | ---: | ---: |"]
    for t, label in zip(TASKS, CN_TASKS):
        report.append("| " + " | ".join([label] + [f"{differences[t,m]:+.2f}" for m in METHODS[3:]]) + " |")
    report += ["", "## 可更新的描述", "",
               "在文件提供的模拟回报列中，完整方法的九任务均值均高于另外五个条件，三类消融差值均为正。这是该列的描述统计，尚不能据此更新实测性能结论。",
               "旧文中的“追逐未体现一致收益”“回报持平”“奖励更新收益不稳定”描述的是原单回合数据，不能直接与新模拟列混作同一批结果。", "",
               "## Token 与机制记录", ""]
    for t, label in [("circle", "环形编队"), ("line", "线形编队")]:
        full = original[t, METHODS[2]]
        catalog = original[t, METHODS[3]]
        saving = 100 * (1 - float(full["accounted_tokens"]) / float(catalog["accounted_tokens"]))
        report.append(f"- {label}原单回合归属 token 节省 {saving:.2f}%，完整方法 {int(full['accounted_tokens']):,}，全技能目录 {int(catalog['accounted_tokens']):,}。")
    report += ["", "新文件每组 token、轮数、模型、错误标注和重排序次数均不随模拟序号变化，因此不能据此给出这些指标的 10 次实测波动或累计机制触发次数。",
               "540 行只有 54 个不同 episode_id，seed 均为 66001；组内变化列只有 sim_run_id 和 simulated_controlled_team_mean_return。",
               "原 14 个保留回合和 40 个新增回合是历史批次信息，不能扩大为本次已经核验 540 次独立执行。", "",
               "## 待确认", "", "需要确认模拟列及标注是否误命名，并提供各次实测记录的对应关系。当前按原 PDF 第 1、2 页理解图一和图二；如指其他图片，需以对应图片为准。", ""]
    (OUT / "RESULTS_SUMMARY_10_RUNS_DRAFT.zh-CN.md").write_text("\n".join(report), encoding="utf-8")
    audit = {"source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
             "metric": METRIC, "data_status": "source_labeled_simulated_pending_confirmation",
             "rows": len(rows), "unique_episode_ids": len({r["episode_id"] for r in rows}),
             "groups": [{"scenario": t, "method": m, **s} for (t, m), s in stats.items()],
             "ablation_differences": [{"scenario": t, "ablation": m, "mean_difference": d}
                                      for (t, m), d in differences.items()]}
    (OUT / "calculated_statistics.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT), "groups": len(stats), "comparisons": len(differences)}))


if __name__ == "__main__":
    main()
