# /// script
# requires-python = ">=3.10"
# dependencies = ["matplotlib>=3.8", "scipy>=1.11"]
# ///
"""Figures for the thesis, from the run records of one phase (reads the records, changes nothing).

    uv run --script pilot/figures.py [--phase pilot|main|test] [--runs DIR] [--out DIR] [--prefix P] [--allow-synthetic]

Uses the valid, non-excluded runs of the phase and pilot/analysis.py's numbers (which also writes the tables). The
agents are drawn as the grid of section 5.1.5: the two agents of a harness pair (same LLM) side by side, the native
harness as a filled marker, OpenCode as an open one. Writes to OUT (default ~/.naoqi-pilot/grid/PHASE/figures):
  PREFIXsuccess.pdf    success rate with a 95 % Wilson score interval, one panel per simulated task
  PREFIXcost.pdf       success rate against median cost per run, one panel per task; the four harness pairs joined
  PREFIXtime.pdf       completion time of every successful run, with the median, one panel per task
  PREFIXpaths-TASK.pdf the torso's path on the floor plan, one panel per agent (rows: LLM, columns: native harness,
                       OpenCode); runs without a recorded path show only the pose at rest; for describing failures
  PREFIXtools-TASK.pdf per category (each tool, obstacle outcomes, falls, refused and rejected calls), every
                       agent's runs side by side in run order, pairs grouped; descriptive, not a metric
"""
import argparse
import math
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
import analysis  # noqa: E402
import dashboard  # noqa: E402
import records  # noqa: E402
from agent_container import AGENT_IDS, AGENTS  # noqa: E402

TASKS = analysis.TASKS
PAIRS = analysis.PAIRS  # (native, OpenCode), C3-C6
LLM_SHORT = {a: AGENTS[a]["llm"].replace("Claude ", "") for a in AGENT_IDS}
HARNESS = {"claude-code": "Claude Code", "opencode": "OpenCode", "agy": "Antigravity"}
NATIVE = {a for a, _ in PAIRS}

# Print, light only: ink for data, one accent for failed runs, recessive grid (dataviz reference palette).
INK, INK_2, MUTED, GRID, WALL, AREA, ACCENT = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#b9b8b1", "#e8f3e8", "#eb6834"
plt.rcParams.update({
    "font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"], "font.size": 8,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK_2, "axes.linewidth": 0.6, "xtick.color": INK_2,
    "ytick.color": INK_2, "xtick.major.width": 0.6, "ytick.major.width": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "pdf.fonttype": 42, "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
})
WIDTH = 6.0  # inches, about the text width of the thesis
# Row of each agent: the pairs one after the other, with a gap between pairs.
ROW = {a: 3 * i + j for i, pair in enumerate(PAIRS) for j, a in enumerate(pair)}


def marker(a, colour=INK, size=5):
    """Native harness filled, OpenCode open."""
    native = a in NATIVE  # the filled marker gets a surface ring, so it is drawn a little larger to read the same
    return dict(marker="o", markersize=size + 1.2 if native else size, color=colour,
                markeredgecolor="white" if native else colour, markerfacecolor=colour if native else "white",
                markeredgewidth=1 if native else 1.2, linestyle="none", clip_on=False)


def harness_legend(fig, extra=(), **kw):
    handles = [Line2D([], [], **marker(PAIRS[0][0])), Line2D([], [], **marker(PAIRS[0][1]))] + [h for h, _ in extra]
    labels = ["native harness (Claude Code, Antigravity)", "OpenCode"] + [t for _, t in extra]
    fig.legend(handles, labels, frameon=False, ncol=len(handles), **kw)


def agent_axis(ax, labels=True):
    """Agents top to bottom in grid order; each pair's LLM written once, left of its two rows. With shared y axes,
    call it with labels=True for the leftmost panel only (setting the ticks again would clear its labels)."""
    if labels:
        ax.set_yticks([ROW[a] for a in AGENT_IDS], ["%s %s" % (a, HARNESS[AGENTS[a]["harness"]]) for a in AGENT_IDS])
    ax.set_ylim(max(ROW.values()) + 0.6, -0.6)
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    if labels:
        for native, _ in PAIRS:
            ax.annotate(LLM_SHORT[native], xy=(0, ROW[native] + 0.5), xycoords=("axes fraction", "data"),
                        xytext=(-78, 0), textcoords="offset points", ha="right", va="center", color=INK, fontsize=8)


def fig_success(result, path):
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.8), sharey=True)
    for ax, (key, name) in zip(axes, TASKS):
        for a in AGENT_IDS:
            s = result["summary"][key][a]
            if not s["n"]:
                ax.text(0.01, ROW[a], "no valid run", va="center", ha="left", color=MUTED, fontsize=7)
                continue
            lo, hi = s["wilson95"]
            ax.plot([lo, hi], [ROW[a]] * 2, color=INK_2, linewidth=1.5, solid_capstyle="round")
            ax.plot(s["rate"], ROW[a], **marker(a))
            ax.text(1.04, ROW[a], "%d/%d" % (s["k"], s["n"]), va="center", ha="left", color=INK_2, clip_on=False)
        agent_axis(ax, labels=ax is axes[0])
        ax.set_xlim(-0.02, 1.02)
        ax.set_xticks([0, 0.25, 0.5, 0.75, 1], ["0", "0.25", "0.5", "0.75", "1"])
        ax.set_title(name, loc="left", fontsize=9, color=INK)
        ax.set_xlabel("Success rate (95 % Wilson interval)")
    harness_legend(fig, loc="lower center", bbox_to_anchor=(0.55, -0.1))
    fig.subplots_adjust(wspace=0.25)
    fig.savefig(path)
    plt.close(fig)


def place_labels(ax, groups, pts):
    """Label each group at the first of a few offsets whose box hits no point, pair line or earlier label."""
    fig = ax.figure
    fig.canvas.draw()
    to_px = ax.transData.transform
    pt = fig.dpi / 72  # pixels per point
    marks = [to_px(xy) for xy in pts.values()]
    lines = [(to_px(pts[a]), to_px(pts[b])) for a, b in PAIRS if a in pts and b in pts]
    placed = []

    def free(box):
        x0, y0, x1, y1 = box
        if any(x0 - 4 * pt < mx < x1 + 4 * pt and y0 - 4 * pt < my < y1 + 4 * pt for mx, my in marks):
            return False
        if any(not (x1 < b[0] or b[2] < x0 or y1 < b[1] or b[3] < y0) for b in placed):
            return False
        for (ax_, ay_), (bx_, by_) in lines:  # sample the segment
            for t in (i / 20 for i in range(21)):
                if x0 < ax_ + t * (bx_ - ax_) < x1 and y0 < ay_ + t * (by_ - ay_) < y1:
                    return False
        return True

    for g in sorted(groups, key=lambda g: -g["y"]):
        text = ", ".join(g["agents"])
        w, h = (len(text) * 4.6 + 2) * pt, 9 * pt
        cx, cy = to_px((g["x"], g["y"]))
        options = [(5, 3, "left"), (5, -12, "left"), (-5, 3, "right"), (-5, -12, "right"), (5, 10, "left"),
                   (-5, 10, "right"), (5, -18, "left"), (-5, -18, "right")]
        for dx, dy, ha in options + [options[0]]:
            x0 = cx + dx * pt - (w if ha == "right" else 0)
            box = (x0, cy + dy * pt, x0 + w, cy + dy * pt + h)
            if free(box):
                break
        placed.append(box)
        ax.annotate(text, (g["x"], g["y"]), xytext=(dx, dy), textcoords="offset points", ha=ha, va="bottom",
                    color=INK_2)


def fig_cost(result, path):
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.6), sharey=True)
    for ax, (key, name) in zip(axes, TASKS):
        pts = {}
        for a in AGENT_IDS:
            s = result["summary"][key][a]
            if s["n"] and s["cost_usd"]["median"] is not None:
                pts[a] = (s["cost_usd"]["median"], s["rate"])
        for native, opencode in PAIRS:
            if native in pts and opencode in pts:
                ax.plot(*zip(pts[native], pts[opencode]), color=MUTED, linewidth=1, linestyle=(0, (3, 2)), zorder=1)
        x_max = max([x for x, _ in pts.values()] + [0.01]) * 1.15
        ax.set_xlim(0, x_max)
        ax.set_ylim(-0.08, 1.12)
        # Agents whose points (nearly) coincide share one label, so labels never overprint.
        groups = []
        for a, (x, y) in pts.items():
            near = next((g for g in groups if abs(g["x"] - x) < 0.04 * x_max and abs(g["y"] - y) < 0.04), None)
            if near:
                near["agents"].append(a)
            else:
                groups.append({"x": x, "y": y, "agents": [a]})
        for a, (x, y) in pts.items():
            ax.plot(x, y, zorder=2, **marker(a))
        place_labels(ax, groups, pts)
        ax.grid(color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        ax.set_title(name, loc="left", fontsize=9, color=INK)
        ax.set_xlabel("Median cost per run (USD)")
    axes[0].set_ylabel("Success rate")
    fig.legend([Line2D([], [], **marker(PAIRS[0][0])), Line2D([], [], **marker(PAIRS[0][1])),
                Line2D([], [], color=MUTED, linewidth=1, linestyle=(0, (3, 2)))],
               ["native harness", "OpenCode", "same LLM (C3: A1, A2; C4: A3, A4; C5: A5, A6; C6: A7, A8)"],
               loc="upper right", bbox_to_anchor=(1, 1.08), frameon=False, ncol=3)
    fig.subplots_adjust(wspace=0.15)
    fig.savefig(path)
    plt.close(fig)


def fig_time(result, data, path):
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, 2.8), sharey=True)
    for ax, (key, name) in zip(axes, TASKS):
        top = 0
        for a in AGENT_IDS:
            times = sorted(r["completion_time_s"] for r in data.get((key, a), []) if r["task_success"])
            top = max([top] + times)
            # Small vertical offsets so equal times stay visible; the order is the sorted order, not random.
            for j, t in enumerate(times):
                ax.plot(t, ROW[a] + (j - (len(times) - 1) / 2) * 0.08, alpha=0.9, **marker(a, size=4))
            median = result["summary"][key][a]["completion_time_s"]["median"]
            if times:
                ax.plot([median] * 2, [ROW[a] - 0.4, ROW[a] + 0.4], color=ACCENT, linewidth=2, solid_capstyle="round")
            else:
                ax.text(0, ROW[a], "no successful run", va="center", ha="left", color=MUTED, fontsize=7)
        agent_axis(ax, labels=ax is axes[0])
        ax.set_xlim(0, top * 1.08 if top else 1)
        ax.set_title(name, loc="left", fontsize=9, color=INK)
        ax.set_xlabel("Completion time (s), successful runs")
    harness_legend(fig, [(Line2D([], [], color=ACCENT, linewidth=2), "median")], loc="lower center",
                   bbox_to_anchor=(0.55, -0.1))
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
    """One panel per category; in each, every agent's runs side by side in run order, pairs grouped (descriptive)."""
    cols = 2
    rows = math.ceil(len(CATEGORIES) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(WIDTH, 1.15 * rows + 0.4), sharex=True)
    for ax, (name, title, value) in zip(axes.flat, CATEGORIES):
        top = 0
        for agent in AGENT_IDS:
            runs = sorted(data.get((key, agent), []), key=lambda r: r["run_id"])
            width = 0.84 / max(len(runs), 1)
            for j, r in enumerate(runs):
                v = value(r)
                top = max(top, v)
                ax.bar(ROW[agent] - 0.42 + width * (j + 0.5), v, width=width * 0.85,
                       color=INK if r["task_success"] else ACCENT, linewidth=0)
        ax.set_title(title, loc="left", fontsize=8, color=INK,
                     fontfamily="monospace" if name in TOOLS or name == "obstacle" else None)
        for native, _ in PAIRS[1:]:
            ax.axvline(ROW[native] - 1, color=GRID, linewidth=0.6, zorder=0)
        ax.set_xlim(-0.6, max(ROW.values()) + 0.6)
        ax.set_xticks([ROW[a] for a in AGENT_IDS], AGENT_IDS, fontsize=7)
        ax.tick_params(axis="x", length=0)
        ax.set_ylim(0, max(1, top) * 1.1)
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(3, integer=True))
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
    for ax in list(axes.flat)[len(CATEGORIES):]:
        ax.axis("off")
    fig.legend([Patch(facecolor=INK), Patch(facecolor=ACCENT)], ["successful run", "failed run"],
               loc="upper right", bbox_to_anchor=(1, 1.0), ncol=2, frameon=False, fontsize=7)
    fig.supylabel("Count per run", fontsize=8, color=INK_2)
    fig.supxlabel("Pairs: A1, A2 Opus 5.5 · A3, A4 Sonnet 5.5 · A5, A6 Haiku 5.5 · A7, A8 Gemini 3.8 Flash "
                  "(odd: native harness, even: OpenCode)", fontsize=7, color=INK_2)
    fig.subplots_adjust(wspace=0.15, hspace=0.6, top=0.94)
    fig.savefig(path)
    plt.close(fig)


def fig_paths(result, data, key, path):
    world = dashboard.world_geometry(key)
    xs = [v for w in world["walls"] for v in (w["x0"], w["x1"])]
    ys = [v for w in world["walls"] for v in (w["y0"], w["y1"])]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    cols, rows = 2, len(PAIRS)  # columns: native harness, OpenCode; rows: the LLMs
    panel_w = WIDTH / cols
    fig, axes = plt.subplots(rows, cols, figsize=(WIDTH, rows * panel_w * span_y / span_x + 0.8))
    a_ = world["area"]
    for ax, agent in zip(axes.flat, [a for pair in PAIRS for a in pair]):
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
        s = result["summary"][key][agent]
        no_path = sum(1 for r in runs if not r["path"])
        ax.set_title("%s %s, %s  (%d/%d)%s" % (agent, LLM_SHORT[agent], HARNESS[AGENTS[agent]["harness"]], s["k"],
                                               s["n"], "  %d without path" % no_path if no_path else ""),
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
    fig.subplots_adjust(wspace=0.05, hspace=0.15, bottom=0.03)
    fig.savefig(path)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=records.PHASES, default="pilot", help="test: instrumentation checks only")
    parser.add_argument("--runs", help="runs folder (default: the phase's); only runs whose manifest has the phase")
    parser.add_argument("--out", help="default: ~/.naoqi-pilot/grid/PHASE/figures")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--allow-synthetic", action="store_true")
    args = parser.parse_args()
    out = Path(args.out or records.STUDY / args.phase / "figures").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    result, data = analysis.analyse(args.phase, args.runs, args.allow_synthetic)
    p = lambda name: out / (args.prefix + name)  # noqa: E731
    fig_success(result, p("success.pdf"))
    fig_cost(result, p("cost.pdf"))
    fig_time(result, data, p("time.pdf"))
    for key, _ in TASKS:
        fig_paths(result, data, key, p("paths-%s.pdf" % key.replace("_", "-")))
        fig_tools(data, key, p("tools-%s.pdf" % key.replace("_", "-")))
    print(out)


if __name__ == "__main__":
    main()
