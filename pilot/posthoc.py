# /// script
# requires-python = ">=3.10"
# dependencies = ["scipy>=1.11"]
# ///
"""Post-hoc analyses of the main runs, not part of the planned analysis (section 4.10); the thesis reports them as
post-hoc in section 5.6 (reads the records, changes nothing). Added 2026-11-02 for the Discussion (author).

    uv run --script pilot/posthoc.py [--phase main] [--out DIR]

Uses the same valid, non-excluded runs as pilot/analysis.py. Writes OUT/posthoc.json and two LaTeX tabulars:
  posthoc-time.tex  per task and agent, over the successful runs (as completion time): medians of the completion
                    time, the time at the bridge (the union of the intervals from a call's arrival at the bridge to
                    its result, up to the first rest), the time between tool calls (completion time minus the time
                    at the bridge: the LLM, the harness, the network and the MCP server), the LLM's turns up to and
                    including the one with the first rest (Claude Code: assistant message IDs; OpenCode: message IDs
                    of its steps; agy: PLANNER_RESPONSE entries), and the time between tool calls per turn
  posthoc-d.tex     successes per agent on the simulated tasks if the success area had depth d = 0.75, 1.0, 1.25 or
                    1.5 m instead of 1.0 m (pilot/success.py with another d; same stop signal, upright and transcript
                    check), and Barnard's exact test of C3-C6 at each d
Plus, in posthoc.json only: the distances of the failed runs that stopped beyond 1.0 m; A7's view_file calls on the
server's instructions and on images, against its NAO tool results that carry an image; and, for the OpenCode
agents, success in runs with and without a refused call.
"""
import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import analysis  # noqa: E402
import metrics  # noqa: E402
import records  # noqa: E402
import success  # noqa: E402
from agent_container import AGENT_IDS, AGENTS  # noqa: E402

D_VALUES = (0.75, 1.0, 1.25, 1.5)
REST = {"claude-code": "mcp__naoqi__rest", "opencode": "naoqi_rest", "agy": "mcp_naoqi_rest"}


def turns_until_rest(harness, agent_dir):
    """The LLM's turns up to and including the turn that holds the first rest call (None if not readable)."""
    seen, ids = set(), []  # turn IDs in order, and whether a turn held rest
    rest_turn = None
    if harness == "agy":
        n = 0
        for path in sorted(agent_dir.glob("agy-brain/*/.system_generated/logs/transcript_full.jsonl")):
            for e in metrics.read_jsonl_lenient(path):
                if e.get("type") == "PLANNER_RESPONSE":
                    n += 1
                    if any(c.get("name") == REST[harness] for c in e.get("tool_calls") or []):
                        return n
        return None
    for e in metrics.read_jsonl_lenient(agent_dir / "transcript.jsonl"):
        if harness == "claude-code" and e.get("type") == "assistant":
            msg = e.get("message") or {}
            turn, tools = msg.get("id"), [b.get("name") for b in msg.get("content") or []
                                          if isinstance(b, dict) and b.get("type") == "tool_use"]
        elif harness == "opencode" and e.get("type") in ("step_start", "tool_use"):
            part = e.get("part") or {}
            turn, tools = part.get("messageID"), [part.get("tool")] if e["type"] == "tool_use" else []
        else:
            continue
        if turn and turn not in seen:
            seen.add(turn)
            ids.append(turn)
        if REST[harness] in tools and rest_turn is None:
            rest_turn = turn
    return ids.index(rest_turn) + 1 if rest_turn else None


def time_split(run_dir, m):
    """Seconds at the bridge and between tool calls, and turns, for a run that ended with rest."""
    calls = sorted((c for c in metrics.read_jsonl(run_dir / "calls.jsonl") if c["type"] == "call"),
                   key=lambda c: c["received"])
    first_rest = next(c for c in calls if c["tool"] == "rest")
    at_bridge, current = 0.0, None
    for c in (c for c in calls if c["received"] <= first_rest["received"]):
        if current and c["received"] <= current[1]:
            current[1] = max(current[1], c["returned"])
        else:
            at_bridge += (current[1] - current[0]) if current else 0.0
            current = [c["received"], c["returned"]]
    at_bridge += current[1] - current[0]
    t = m["completion_time_s"]
    harness = json.loads((run_dir / "manifest.json").read_text())["agent_config"]["harness"]
    turns = turns_until_rest(harness, run_dir / "agent")
    return {"completion_time_s": t, "at_bridge_s": at_bridge, "between_calls_s": t - at_bridge, "turns": turns,
            "between_calls_per_turn_s": (t - at_bridge) / turns if turns else None}


def med(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def a7_view_file(run_dir):
    instructions = images = image_results = 0
    for path in sorted((run_dir / "agent").glob("agy-brain/*/.system_generated/logs/transcript_full.jsonl")):
        for e in metrics.read_jsonl_lenient(path):
            if e.get("type") != "PLANNER_RESPONSE":
                continue
            for c in e.get("tool_calls") or []:
                name, target = c.get("name") or "", str((c.get("args") or {}).get("AbsolutePath", ""))
                if name == "view_file" and target.endswith("/instructions.md"):
                    instructions += 1
                elif name == "view_file" and "/media_" in target:
                    images += 1
                elif name.startswith("mcp_naoqi_") and name != "mcp_naoqi_say":
                    image_results += 1  # every NAO tool result except say's carries an image (errors aside)
    return {"instructions": instructions, "images": images, "nao_results_with_image": image_results}


def analyse(phase):
    runs = [(man, m) for man, m in analysis.load(phase) if m["valid"] and not m["excluded"]]
    folder = records.runs_dir(phase)
    out = {"phase": phase, "time": {}, "d": {}, "failed_beyond_1m": {}, "a7_view_file": {}, "opencode_refusals": {}}
    for key, _ in analysis.TASKS:
        task_runs = [(man, m) for man, m in runs if man["task_key"] == key]
        out["time"][key] = {}
        for a in AGENT_IDS:
            ok = [time_split(folder / m["run_id"], m) for man, m in task_runs if man["agent"] == a and m["task_success"]]
            out["time"][key][a] = {"n_successful": len(ok), "n": sum(man["agent"] == a for man, _ in task_runs),
                                   **{k: med(r[k] for r in ok) for k in ("completion_time_s", "at_bridge_s",
                                                                          "between_calls_s", "turns",
                                                                          "between_calls_per_turn_s")}}
        if key in dict(analysis.SIM_TASKS):
            per_d = {}
            for d in D_VALUES:
                k = Counter()
                for man, m in task_runs:
                    gt_file = folder / m["run_id"] / "ground_truth.json"
                    v = success.verdict(key, json.loads(gt_file.read_text()), d) \
                        if m["success_check"]["ended_with_rest"] else None
                    k[man["agent"]] += bool(v and v["success"]) and not m["success_check"]["protocol_violation"]
                n = Counter(man["agent"] for man, _ in task_runs)
                tests = {c: analysis.barnard(k[x], n[x], k[y], n[y]) for c, (x, y) in analysis.TESTED.items()}
                per_d[str(d)] = {"successes": {a: k[a] for a in AGENT_IDS}, "n": {a: n[a] for a in AGENT_IDS},
                                 "barnard_p": tests}
            out["d"][key] = per_d
            out["failed_beyond_1m"][key] = sorted(
                [[man["agent"], m["distance_to_goal_m"]] for man, m in task_runs
                 if not m["task_success"] and (m["distance_to_goal_m"] or 0) > 1.0], key=lambda x: x[1])
        a7 = [a7_view_file(folder / m["run_id"]) for man, m in task_runs if man["agent"] == "A7"]
        out["a7_view_file"][key] = {"runs": len(a7), "runs_opening_instructions": sum(r["instructions"] > 0 for r in a7),
                                    **{k: sum(r[k] for r in a7) for k in ("instructions", "images",
                                                                         "nao_results_with_image")}}
    oc = [m for man, m in runs if AGENTS[man["agent"]]["harness"] == "opencode"]
    out["opencode_refusals"] = {
        "runs_with_refused_call": [sum(m["task_success"] for m in oc if m["tool_calls"]["refused"]),
                                   sum(1 for m in oc if m["tool_calls"]["refused"])],
        "runs_without": [sum(m["task_success"] for m in oc if not m["tool_calls"]["refused"]),
                         sum(1 for m in oc if not m["tool_calls"]["refused"])]}
    return out


def latex(out):
    label = lambda a: "%s & %s & %s" % (a, AGENTS[a]["llm"].replace("Claude ", ""),  # noqa: E731
                                        {"claude-code": "Claude Code", "opencode": "OpenCode", "agy": "Antigravity"}
                                        [AGENTS[a]["harness"]])
    head0 = "\\textbf{Agent} & \\textbf{LLM} & \\textbf{Harness}"
    f = analysis.f
    time_rows = [(name, [[label(a), "%d/%d" % (t["n_successful"], t["n"]), f(t["completion_time_s"], "%.0f"),
                          f(t["at_bridge_s"], "%.0f"), f(t["between_calls_s"], "%.0f"), f(t["turns"], "%.0f"),
                          f(t["between_calls_per_turn_s"], "%.1f")]
                         for a in AGENT_IDS for t in [out["time"][key][a]]]) for key, name in analysis.TASKS]
    res = {"posthoc-time.tex": analysis.tabular(
        "lllcccccc", head0 + " & \\textbf{Successes} & \\textbf{Time} & \\textbf{At the bridge} & "
        "\\textbf{Between calls} & \\textbf{Turns} & \\textbf{Per turn}", time_rows)
        .replace("pilot/analysis.py", "pilot/posthoc.py")}
    cols = "lll" + "c" * (2 * len(D_VALUES))
    lines = ["%% Generated by pilot/posthoc.py from the run records; do not edit by hand.",
             "\\begin{tabular}{@{}%s@{}}" % cols, "\\toprule",
             " & & & " + " & ".join("\\multicolumn{%d}{c}{\\textbf{%s}}" % (len(D_VALUES), name)
                                    for _, name in analysis.SIM_TASKS) + " \\\\",
             "\\cmidrule(lr){4-%d} \\cmidrule(l){%d-%d}" % (3 + len(D_VALUES), 4 + len(D_VALUES), 3 + 2 * len(D_VALUES)),
             head0 + " & " + " & ".join("%s~m" % d for _ in analysis.SIM_TASKS for d in D_VALUES) + " \\\\",
             "\\midrule"]
    for i, a in enumerate(AGENT_IDS):
        cells = ["%d" % out["d"][key][str(d)]["successes"][a] for key, _ in analysis.SIM_TASKS for d in D_VALUES]
        lines.append(("\\addlinespace[2pt]" if i and i % 2 == 0 else "") + label(a) + " & " + " & ".join(cells) + " \\\\")
    lines.append("\\midrule")
    for c, (x, y) in analysis.TESTED.items():
        cells = ["%.3f" % out["d"][key][str(d)]["barnard_p"][c] for key, _ in analysis.SIM_TASKS for d in D_VALUES]
        lines.append("\\multicolumn{3}{@{}l}{%s, $p$ (%s vs.\\ %s)} & " % (c, x, y) + " & ".join(cells) + " \\\\")
    res["posthoc-d.tex"] = "\n".join(lines + ["\\bottomrule", "\\end{tabular}"]) + "\n"
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=records.PHASES, default="main")
    parser.add_argument("--out", help="default: ~/.naoqi-pilot/grid/PHASE/analysis")
    args = parser.parse_args()
    out_dir = Path(args.out or records.STUDY / args.phase / "analysis").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = analyse(args.phase)
    (out_dir / "posthoc.json").write_text(json.dumps(out, indent=2))
    for name, text in latex(out).items():
        (out_dir / name).write_text(text)
    print(out_dir)


if __name__ == "__main__":
    main()
