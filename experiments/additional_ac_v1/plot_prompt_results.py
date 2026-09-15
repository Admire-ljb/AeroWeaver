"""Plot measured prompt sensitivity with sample SD and all paired seed values."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with args.source.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["experiment"] == "C"]
    tasks = ("world_communication", "collection")
    labels = ("World communication", "Collection-delivery")
    conditions = ("original", "paraphrased", "reordered", "distractors")
    seeds = ("66201", "66202", "66203")
    assert len(rows) == 24
    assert all(r["status"] in {"horizon", "complete"} for r in rows)
    lookup = {(r["scenario"], r["condition"], r["seed"]): r for r in rows}
    assert len(lookup) == 24
    for task in tasks:
        for seed in seeds:
            assert len({lookup[task, c, seed]["initial_state_sha256"] for c in conditions}) == 1
    with (args.output / "source_data.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    plt.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": ["Arial", "DejaVu Sans"],
        "font.size": 8, "axes.labelsize": 9, "xtick.labelsize": 8,
        "ytick.labelsize": 8, "axes.spines.top": False, "axes.spines.right": False,
        "axes.linewidth": .65, "pdf.fonttype": 42, "svg.fonttype": "none",
        "text.color": "#252525", "axes.labelcolor": "#252525",
        "xtick.color": "#454545", "ytick.color": "#454545",
    })
    colors = ("#8796A5", "#4483B0", "#D99D45", "#549782")
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.85))
    fig.subplots_adjust(left=.077, right=.992, top=.955, bottom=.235, wspace=.265)
    summaries = []
    for panel, (ax, task, label) in enumerate(zip(axes, tasks, labels)):
        data = np.array([[float(lookup[task, c, s]["controlled_team_mean_return"]) for s in seeds] for c in conditions])
        means = data.mean(axis=1)
        std = data.std(axis=1, ddof=1)
        ax.set_axisbelow(True)
        ax.grid(axis="y", color="#E6E8EA", linewidth=.55)
        ax.axhline(0, color="#737373", linewidth=.7)
        ax.bar(np.arange(4), means, width=.6, color=colors, alpha=.8,
               edgecolor=colors, linewidth=.7, zorder=2)
        ax.errorbar(np.arange(4), means, yerr=std, fmt="none", ecolor="#292929",
                    elinewidth=.9, capsize=3, capthick=.9, zorder=4)
        for index, (offset, marker) in enumerate(zip((-.14, 0, .14), ("o", "s", "^"))):
            ax.scatter(np.arange(4) + offset, data[:, index], s=17, marker=marker,
                       facecolors="white", edgecolors="#333333", linewidths=.65, zorder=5)
        ax.set_xticks(range(4), [c.capitalize() for c in conditions])
        ax.tick_params(axis="x", length=0, pad=6)
        ax.tick_params(axis="y", width=.65, length=3)
        ax.set_xlim(-.58, 3.58)
        ax.set_ylabel("Task return")
        ax.set_ylim((-15, 135) if panel == 0 else (-18, 78))
        ax.set_yticks((0, 30, 60, 90, 120) if panel == 0 else (-15, 0, 15, 30, 45, 60, 75))
        for x, mean, sd, values in zip(range(4), means, std, data):
            ax.text(x, max(mean + sd, max(values)) + (4 if panel == 0 else 2.7),
                    f"{mean:.2f}", ha="center", va="bottom", fontsize=8)
        ax.text(.5, -.235, f"({chr(97 + panel)}) {label}", transform=ax.transAxes,
                ha="center", va="top", fontsize=9)
        for condition, mean, sd in zip(conditions, means, std):
            summaries.append(dict(task=task, condition=condition, n=3, mean=float(mean), sd=float(sd)))
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    width, height = fig.canvas.get_width_height()
    for text in fig.findobj(matplotlib.text.Text):
        if text.get_visible() and text.get_text():
            box = text.get_window_extent(renderer)
            assert box.x0 >= -1 and box.y0 >= -1 and box.x1 <= width+1 and box.y1 <= height+1, text.get_text()
    pixels = np.asarray(fig.canvas.buffer_rgba())[:, :, :3]
    assert np.mean(np.any(pixels < 230, axis=2)) > .05
    for suffix in ("png", "pdf", "svg"):
        fig.savefig(args.output / f"prompt_sensitivity_measured.{suffix}", dpi=300, facecolor="white")
    plt.close(fig)
    caption = (
        "Prompt sensitivity of AeroWeaver on (a) world communication and (b) collection-delivery. "
        "Bars and labels show mean task return; error bars indicate sample standard deviation across three paired "
        "environment seeds (n=3). Open circles, squares, and triangles denote seeds 66201, 66202, and 66203, respectively, "
        "with fixed horizontal offsets used only for visibility. Each condition uses fresh memory and shared task-level "
        "skill activation; only the mission text at local action selection is perturbed. The two panels use different "
        "return scales. All 24 completed episodes, including decision and invocation errors, are retained. "
        "These are measured Mock-runtime results, not native MPE rollouts. Requested model: deepseek-v4-flash; "
        "returned model identifier: deepseek-flash. No statistical significance claim is made.\n"
    )
    (args.output / "caption.txt").write_text(caption, encoding="utf-8")
    metadata = dict(
        conclusion="Sensitivity differs by task and seed; these results do not establish uniform prompt robustness.",
        archetype="quantitative grid", dimensions_inches=[7.2, 2.85], source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
        rows=24, summaries=summaries, mean_sd_recomputed=True, paired_initial_states_verified=True,
        nonblank_and_text_bounds_checked=True,
    )
    (args.output / "figure_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
