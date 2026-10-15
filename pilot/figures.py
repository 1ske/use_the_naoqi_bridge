# /// script
# requires-python = ">=3.10"
# dependencies = ["matplotlib>=3.8"]
# ///
"""Figures and tables for the thesis, from the run records (reads the records, changes nothing).

    uv run --script pilot/figures.py [--runs ~/.naoqi-pilot/runs] [--out DIR] [--prefix pilot-]

Uses valid, non-excluded, non-test runs only, with pilot/metrics.py's values (section 5.1.4). Writes to OUT:
  PREFIXtable.tex      per task and agent: successes/runs, median completion time and tool calls over successful
                       runs, median cost over all runs, falls (a whole booktabs tabular; \\input it outside a tabular)
  PREFIXsuccess.pdf    success rate with a 95 % Wilson score interval, one panel per simulated task
  PREFIXcost.pdf       success rate against median cost per run, one panel per task; A1-A5 and A2-A6 joined
  PREFIXtime.pdf       completion time of every successful run, with the median, one panel per task
  PREFIXpaths-TASK.pdf the torso's path on the floor plan, one panel per agent (runs without a recorded path show
                       only the pose at rest); for describing failures, not a metric
  PREFIXtools-TASK.pdf per category (each tool, obstacle outcomes, falls, refused and rejected calls), every
                       agent's runs side by side in run order; descriptive, not a metric
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import dashboard  # noqa: E402
import metrics  # noqa: E402

AGENTS = ["A1", "A2", "A3", "A4", "A5", "A6"]
TASKS = [("corridor", "Corridor"), ("corridor_complex", "Complex corridor")]
HARNESS_PAIRS = [("A1", "A5"), ("A2", "A6")]  # C2 and C3 (Table 5.5)

# Print, light only: ink for data, one accent for failed runs, recessive grid (dataviz reference palette).
INK, INK_2, MUTED, GRID, WALL, AREA, ACCENT = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#b9b8b1", "#e8f3e8", "#eb6834"
plt.rcParams.update({
    "font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"], "font.size": 8,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK_2, "axes.linewidth": 0.6, "xtick.color": INK_2,
    "ytick.color": INK_2, "xtick.major.width": 0.6, "ytick.major.width": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "pdf.fonttype": 42, "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
})
WIDTH = 6.0  # inches, about the text width of the thesis


def wilson(k, n, z=1.959964):
    """95 % Wilson score interval for k successes in n runs (section 5.1.10)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z / (1 + z * z / n) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def load(runs_dir):
    """Metrics of the runs that count, keyed by (task_key, agent), plus each run's path from trajectory.jsonl."""
    out = {}
    for manifest in sorted(Path(runs_dir).expanduser().glob("*/manifest.json")):
        run_dir = manifest.parent
        man = json.loads(manifest.read_text())
        if man.get("test") or man["run_id"].endswith("-test"):
            continue
        m = metrics.metrics(run_dir)
        if not m["valid"] or m["excluded"]:
            continue
        m["path"] = [(s["x"], s["y"]) for s in metrics.read_jsonl_lenient(run_dir / "trajectory.jsonl")] \
            if (run_dir / "trajectory.jsonl").exists() else []
        out.setdefault((man["task_key"], man["agent"]), []).append(m)
    return out


def summary(runs):
    ok = [r for r in runs if r["task_success"]]
    costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
    return {
        "n": len(runs), "k": len(ok),
        "time": statistics.median([r["completion_time_s"] for r in ok]) if ok else None,
        "calls": statistics.median([r["tool_calls"]["total"] for r in ok]) if ok else None,
        "cost": statistics.median(costs) if costs else None, "cost_missing": len(runs) - len(costs),
        "falls": sum(r["safety_events"]["falls"] for r in runs),
        "times": [r["completion_time_s"] for r in ok],
    }


def table(data):
    fmt = lambda v, f: "--" if v is None else f % v
    rows = []
    for key, name in TASKS:
        rows.append("\\multicolumn{6}{@{}l}{\\textit{%s}} \\\\" % name)
        for a in AGENTS:
            s = summary(data.get((key, a), []))
            cost = fmt(s["cost"], "%.2f") + ("\\textsuperscript{*}" if s["cost_missing"] else "")
            rows.append("%s & %d/%d & %s & %s & %s & %d \\\\" % (
                a, s["k"], s["n"], fmt(s["time"], "%.0f"), fmt(s["calls"], "%.0f"), cost, s["falls"]))
        rows.append("\\addlinespace")
    head = ["%% Generated by pilot/figures.py from the run records; do not edit by hand.",
            "\\begin{tabular}{@{}lccccc@{}}", "\\toprule",
            "\\textbf{Agent} & \\textbf{Successes} & \\textbf{Time (s)} & \\textbf{Tool calls} & \\textbf{Cost (USD)}"
            " & \\textbf{Falls} \\\\", "\\midrule"]
    return "\n".join(head + rows[:-1] + ["\\bottomrule", "\\end{tabular}"]) + "\n"


def agent_axis(ax):
    ax.set_yticks(range(len(AGENTS)), AGENTS)
    ax.set_ylim(len(AGENTS) - 0.5, -0.5)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def fig_success(data, path):
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.2), sharey=True)
    for ax, (key, name) in zip(axes, TASKS):
        for i, a in enumerate(AGENTS):
            s = summary(data.get((key, a), []))
            if not s["n"]:
                continue
            lo, hi = wilson(s["k"], s["n"])
            ax.plot([lo, hi], [i, i], color=INK_2, linewidth=1.5, solid_capstyle="round")
            ax.plot(s["k"] / s["n"], i, "o", color=INK, markersize=5, markeredgecolor="white", markeredgewidth=1)
            ax.text(1.04, i, "%d/%d" % (s["k"], s["n"]), va="center", ha="left", color=INK_2, clip_on=False)
        agent_axis(ax)
        ax.set_xlim(0, 1)
        ax.set_xticks([0, 0.25, 0.5, 0.75, 1], ["0", "0.25", "0.5", "0.75", "1"])
        ax.set_title(name, loc="left", fontsize=9, color=INK)
        ax.set_xlabel("Success rate (95 % Wilson interval)")
    fig.subplots_adjust(wspace=0.3)
    fig.savefig(path)
    plt.close(fig)


def fig_cost(data, path):
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.4), sharey=True)
    for ax, (key, name) in zip(axes, TASKS):
        pts = {}
        for a in AGENTS:
            s = summary(data.get((key, a), []))
            if s["n"] and s["cost"] is not None:
                pts[a] = (s["cost"], s["k"] / s["n"])
        for a, b in HARNESS_PAIRS:
            if a in pts and b in pts:
                ax.plot(*zip(pts[a], pts[b]), color=MUTED, linewidth=1, linestyle=(0, (3, 2)), zorder=1)
        x_max = max([x for x, _ in pts.values()] + [0.01]) * 1.15
        # Agents whose points (nearly) coincide share one label, so labels never overprint.
        groups = []
        for a, (x, y) in pts.items():
            near = next((g for g in groups if abs(g["x"] - x) < 0.04 * x_max and abs(g["y"] - y) < 0.04), None)
            if near:
                near["agents"].append(a)
            else:
                groups.append({"x": x, "y": y, "agents": [a]})
        for a, (x, y) in pts.items():
            ax.plot(x, y, "o", color=INK, markersize=5, markeredgecolor="white", markeredgewidth=1, zorder=2)
        for g in groups:
            # A1 and A2 are labelled on the left, away from the line to their native-harness partner.
            left = g["agents"] in (["A1"], ["A2"])
            ax.annotate(", ".join(g["agents"]), (g["x"], g["y"]), xytext=(-4 if left else 4, 4),
                        textcoords="offset points", ha="right" if left else "left", color=INK_2)
        ax.set_xlim(0, x_max)
        ax.set_ylim(-0.08, 1.12)
        ax.grid(color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        ax.set_title(name, loc="left", fontsize=9, color=INK)
        ax.set_xlabel("Median cost per run (USD)")
    axes[0].set_ylabel("Success rate")
    fig.legend([Line2D([], [], color=MUTED, linewidth=1, linestyle=(0, (3, 2)))],
               ["same LLM in OpenCode and in its native harness (C2: A1, A5; C3: A2, A6)"],
               loc="upper right", bbox_to_anchor=(1, 1.06), frameon=False)
    fig.subplots_adjust(wspace=0.15)
    fig.savefig(path)
    plt.close(fig)


def fig_time(data, path):
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.2), sharey=True)
    for ax, (key, name) in zip(axes, TASKS):
        top = 0
        for i, a in enumerate(AGENTS):
            s = summary(data.get((key, a), []))
            times = sorted(s["times"])
            top = max([top] + times)
            # Small vertical offsets so equal times stay visible; the order is the sorted order, not random.
            for j, t in enumerate(times):
                ax.plot(t, i + (j - (len(times) - 1) / 2) * 0.12, "o", color=INK, markersize=4,
                        markeredgecolor="white", markeredgewidth=0.8, alpha=0.9)
            if times:
                ax.plot([s["time"]] * 2, [i - 0.3, i + 0.3], color=ACCENT, linewidth=2, solid_capstyle="round")
            else:
                ax.text(0, i, "no successful run", va="center", ha="left", color=MUTED, fontsize=7)
        agent_axis(ax)
        ax.set_xlim(0, top * 1.08 if top else 1)
        ax.set_title(name, loc="left", fontsize=9, color=INK)
        ax.set_xlabel("Completion time (s), successful runs")
    axes[1].legend([Line2D([], [], color=ACCENT, linewidth=2)], ["median"], loc="lower right", frameon=False)
    fig.subplots_adjust(wspace=0.15)
    fig.savefig(path)
    plt.close(fig)


TOOLS = ["get_state", "look", "look_around", "stand", "walk_to", "go", "say", "rest"]
CATEGORIES = [(t, t, lambda r, t=t: r["tool_calls"]["by_tool"].get(t, 0)) for t in TOOLS] + [
    ("obstacle", "obstacle outcomes", lambda r: r["safety_events"]["obstacle"]),
    ("falls", "falls", lambda r: r["safety_events"]["falls"]),
    ("refused", "refused calls", lambda r: r["tool_calls"]["refused"]),
    ("rejected", "rejected calls", lambda r: r["tool_calls"]["rejected"]),
]


def fig_tools(data, key, path):
    """One panel per category; in each, every agent's runs side by side, in run order (descriptive, not a metric)."""
    cols = 4
    rows = math.ceil(len(CATEGORIES) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(WIDTH, 1.25 * rows + 0.4), sharex=True)
    for ax, (name, title, value) in zip(axes.flat, CATEGORIES):
        top = 0
        for i, agent in enumerate(AGENTS):
            runs = sorted(data.get((key, agent), []), key=lambda r: r["run_id"])
            width = 0.8 / max(len(runs), 1)
            for j, r in enumerate(runs):
                v = value(r)
                top = max(top, v)
                ax.bar(i - 0.4 + width * (j + 0.5), v, width=width * 0.85,
                       color=INK if r["task_success"] else ACCENT, linewidth=0)
        ax.set_title(title, loc="left", fontsize=8, color=INK,
                     fontfamily="monospace" if name in TOOLS or name == "obstacle" else None)
        for i in range(1, len(AGENTS)):
            ax.axvline(i - 0.5, color=GRID, linewidth=0.6, zorder=0)
        ax.set_xlim(-0.5, len(AGENTS) - 0.5)
        ax.set_xticks(range(len(AGENTS)), AGENTS, fontsize=7)
        ax.tick_params(axis="x", length=0)
        ax.set_ylim(0, max(1, top) * 1.1)
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(3, integer=True))
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
    for ax in list(axes.flat)[len(CATEGORIES):]:
        ax.axis("off")
    fig.legend([Patch(facecolor=INK), Patch(facecolor=ACCENT)], ["successful run", "failed run"],
               loc="upper right", bbox_to_anchor=(1, 1.04), ncol=2, frameon=False, fontsize=7)
    fig.supylabel("Count per run", fontsize=8, color=INK_2)
    fig.subplots_adjust(wspace=0.3, hspace=0.5)
    fig.savefig(path)
    plt.close(fig)


def fig_paths(data, key, path):
    world = dashboard.world_geometry(key)
    xs = [v for w in world["walls"] for v in (w["x0"], w["x1"])]
    ys = [v for w in world["walls"] for v in (w["y0"], w["y1"])]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    cols = 3
    panel_w = WIDTH / cols
    fig, axes = plt.subplots(2, cols, figsize=(WIDTH, 2 * panel_w * span_y / span_x + 0.6))
    a_ = world["area"]
    for ax, agent in zip(axes.flat, AGENTS):
        ax.add_patch(Rectangle((a_["x"][0], a_["y"][0]), a_["x"][1] - a_["x"][0], a_["y"][1] - a_["y"][0],
                               facecolor=AREA, edgecolor="none", zorder=0))
        for w in world["walls"]:
            ax.add_patch(Rectangle((w["x0"], w["y0"]), w["x1"] - w["x0"], w["y1"] - w["y0"],
                                   facecolor=INK if w["goal"] else WALL, edgecolor="none", zorder=1))
        ax.plot(*world["start"], "o", markersize=4, markerfacecolor="white", markeredgecolor=INK_2, zorder=3)
        runs = data.get((key, agent), [])
        for r in runs:
            ok = bool(r["task_success"])
            colour, style = (INK, "-") if ok else (ACCENT, (0, (3, 1.5)))
            if r["path"]:
                ax.plot(*zip(*r["path"]), color=colour, linestyle=style, linewidth=1, zorder=2)
            end = (r.get("success_check") or {}).get("torso_xy_m") or (r["path"][-1] if r["path"] else None)
            fell = r["safety_events"]["falls"] > 0
            if end:
                ax.plot(*end, "x" if fell else "o", color=colour, markersize=4, markeredgewidth=1.2, zorder=4)
        s = summary(runs)
        no_path = sum(1 for r in runs if not r["path"])
        ax.set_title("%s  (%d/%d)%s" % (agent, s["k"], s["n"], "  %d without path" % no_path if no_path else ""),
                     loc="left", fontsize=8, color=INK)
        ax.set_aspect("equal")
        ax.set_xlim(min(xs) - 0.1, max(xs) + 0.1)
        ax.set_ylim(min(ys) - 0.1, max(ys) + 0.1)
        ax.axis("off")
    handles = [Line2D([], [], color=INK, linewidth=1), Line2D([], [], color=ACCENT, linewidth=1, linestyle=(0, (3, 1.5))),
               Line2D([], [], marker="o", color=INK_2, linestyle="none", markersize=4),
               Line2D([], [], marker="x", color=INK_2, linestyle="none", markersize=4, markeredgewidth=1.2),
               Patch(facecolor=AREA), Patch(facecolor=INK)]
    fig.legend(handles, ["successful run", "failed run", "pose at rest", "pose at rest, run with a fall",
                         "success area", "goal"], loc="lower center", ncol=6, frameon=False, fontsize=7,
               handlelength=1.6, columnspacing=1.0)
    fig.subplots_adjust(wspace=0.05, hspace=0.15, bottom=0.08)
    fig.savefig(path)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", default=str(dashboard.HOME / "runs"))
    parser.add_argument("--out", default=str(dashboard.HOME / "figures"))
    parser.add_argument("--prefix", default="")
    args = parser.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    data = load(args.runs)
    p = lambda name: out / (args.prefix + name)
    p("table.tex").write_text(table(data))
    fig_success(data, p("success.pdf"))
    fig_cost(data, p("cost.pdf"))
    fig_time(data, p("time.pdf"))
    for key, _ in TASKS:
        fig_paths(data, key, p("paths-%s.pdf" % key.replace("_", "-")))
        fig_tools(data, key, p("tools-%s.pdf" % key.replace("_", "-")))
    print(out)


if __name__ == "__main__":
    main()
