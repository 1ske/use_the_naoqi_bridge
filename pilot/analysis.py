# /// script
# requires-python = ">=3.10"
# dependencies = ["scipy>=1.11"]
# ///
"""The analysis of section 5.1.10, from the run records of one phase (reads the records, changes nothing).

    uv run --script pilot/analysis.py [--phase pilot|main|test] [--runs DIR] [--out DIR]

Uses the valid, non-excluded runs of the phase (test: only to check the instrumentation itself; test runs are never
part of the pilot or the main runs) (pilot/records.py, pilot/metrics.py), each task separately, every
estimate with its number of runs. Search, on the physical NAO, is reported descriptively: in every output except
tests.tex and distance.tex, as its own block. Writes OUT/analysis.json and these LaTeX tabulars (\\input them inside
a table):
  success.tex     per task and agent: successes / valid runs, success rate, 95 % Wilson score interval
  tests.tex       C3-C6: Barnard's exact test on task success, per simulated task (8 tests, unadjusted), and C7: the
                  sign of each native - OpenCode difference in success rate
  efficiency.tex  median and interquartile range of completion time and tool calls (successful runs, with their
                  number) and of cost (all valid runs; runs with a missing cost are left out and counted)
  distance.tex    distance to goal: median and IQR over the valid runs in which it is defined, with their number
  rq3.tex         per task and agent: share of runs with a fall, an obstacle and a bumped outcome and (Search) an
                  intervention; rejected and refused tool calls per run; instruction following; A7's calls to its
                  exempt tools
  checks.tex      instruction and image check, right answers out of the attempts, before and after
Plus, in analysis.json only: valid and invalid runs per agent with the reasons, the end conditions of failed runs,
and token counts (median and IQR over all valid runs; runs with missing counts left out and counted).

Synthetic records (manifest "synthetic": true, pilot/tools/synthetic_runs.py) are refused unless --allow-synthetic.
"""
import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import metrics  # noqa: E402
import records  # noqa: E402
from agent_container import AGENT_IDS, AGENTS  # noqa: E402

SIM_TASKS = [("corridor", "Corridor"), ("corridor_complex", "Complex corridor")]  # tested (C3-C6), distance to goal
SEARCH = [("search", "Search")]  # physical NAO: descriptive only (section 5.1.10)
TASKS = SIM_TASKS + SEARCH
GRID = AGENTS["grid"]
PAIRS = [tuple(p) for p in GRID["pairs"]]  # (native, OpenCode) for C3-C6
TESTED = {c: tuple(GRID["contrasts"][c]) for c in ("C3", "C4", "C5", "C6")}
Z95 = 1.959963984540054
QUANTILES = "statistics.quantiles(method='inclusive'), i.e. linear interpolation between order statistics"


def wilson(k, n, z=Z95):
    """95 % Wilson score interval for k successes in n runs (section 5.1.10)."""
    if n == 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z / (1 + z * z / n) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [max(0.0, centre - half), min(1.0, centre + half)]


def barnard(k1, n1, k2, n2):
    """Two-sided p-value of Barnard's exact test (scipy.stats.barnard_exact, its defaults: Wald statistic with the
    pooled variance, nuisance parameter maximised over 32 points) for successes k1/n1 against k2/n2."""
    from scipy.stats import barnard_exact
    if not n1 or not n2:
        return None
    return float(barnard_exact([[k1, k2], [n1 - k1, n2 - k2]], alternative="two-sided").pvalue)


def median_iqr(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"n": 0, "median": None, "q1": None, "q3": None}
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else values * 3
    return {"n": len(values), "median": statistics.median(values), "q1": q1, "q3": q3}


def load(phase, folder=None, allow_synthetic=False):
    """All runs of the phase with their metrics: [(manifest, metrics)]. Test runs are never part of it."""
    out = []
    for run_dir in records.runs_of(phase, folder):
        manifest = json.loads((run_dir / "manifest.json").read_text())
        if manifest.get("synthetic") and not allow_synthetic:
            raise SystemExit("%s is a synthetic record; pass --allow-synthetic to analyse synthetic records" % run_dir)
        if manifest.get("test") and phase != "test":
            continue  # never part of the pilot or the main runs
        m = metrics.metrics(run_dir)
        check = run_dir / "transcript_check.json"
        m["exempt_calls"] = json.loads(check.read_text()).get("exempt_calls", {}) if check.exists() else {}
        m["path"] = [(s["x"], s["y"]) for s in metrics.read_jsonl_lenient(run_dir / "trajectory.jsonl")]
        out.append((manifest, m))
    return out


def counted(runs):
    """{(task_key, agent): [metrics]} of the runs that count: valid and not excluded."""
    data = {}
    for manifest, m in runs:
        if m["valid"] and not m["excluded"]:
            data.setdefault((manifest["task_key"], manifest["agent"]), []).append(m)
    return data


def summary(data):
    out = {}
    for key, _ in TASKS:
        per_agent = {}
        for a in AGENT_IDS:
            runs = data.get((key, a), [])
            ok = [r for r in runs if r["task_success"]]
            costs = [r["cost_usd"] for r in runs]
            n = len(runs)
            share = lambda f: (sum(1 for r in runs if f(r)) / n) if n else None  # noqa: E731
            per_run = lambda f: (sum(f(r) for r in runs) / n) if n else None  # noqa: E731
            per_agent[a] = {
                "n": n, "k": len(ok), "rate": len(ok) / n if n else None, "wilson95": wilson(len(ok), n),
                "completion_time_s": median_iqr(r["completion_time_s"] for r in ok),
                "tool_calls": median_iqr(r["tool_calls"]["total"] for r in ok),
                "cost_usd": dict(median_iqr(costs), missing=sum(c is None for c in costs)),
                "tokens": {k: dict(median_iqr((r.get("tokens") or {}).get(k) for r in runs),
                                   missing=sum(not r.get("tokens") for r in runs))
                           for k in ("total", "input", "cache_read", "cache_write", "output", "reasoning")},
                "distance_to_goal_m": median_iqr(r["distance_to_goal_m"] for r in runs),
                "rq3": {
                    "runs_with_fall": share(lambda r: r["safety_events"]["falls"] > 0),
                    "runs_with_obstacle": share(lambda r: r["safety_events"]["obstacle"] > 0),
                    "runs_with_bumped": share(lambda r: r["safety_events"]["bumped"] > 0),
                    "runs_with_intervention": share(lambda r: (r["safety_events"]["interventions"] or 0) > 0)
                    if key in dict(SEARCH) else None,
                    "rejected_per_run": per_run(lambda r: r["tool_calls"]["rejected"]),
                    "refused_per_run": per_run(lambda r: r["tool_calls"]["refused"]),
                    "instruction_following": share(lambda r: r["instruction_following"]),
                    "exempt_calls": dict(sum((Counter(r.get("exempt_calls") or {}) for r in runs), Counter())),
                    "exempt_calls_per_run": per_run(lambda r: sum((r.get("exempt_calls") or {}).values())),
                },
                "failed_end_conditions": dict(Counter(r["end_condition"] for r in runs if not r["task_success"])),
            }
        out[key] = per_agent
    return out


def tests(per_task):
    """C3-C6 with Barnard's exact test per simulated task (unadjusted), and C7's signs."""
    out = {"method": "scipy.stats.barnard_exact, two-sided, defaults (pooled Wald statistic, n=32); 8 tests, "
                     "not adjusted for multiple comparisons (section 5.1.10)", "C3-C6": [], "C7": {}}
    for key, _ in SIM_TASKS:
        signs = {}
        for c, (native, opencode) in TESTED.items():
            a, b = per_task[key][native], per_task[key][opencode]
            diff = a["rate"] - b["rate"] if a["n"] and b["n"] else None
            out["C3-C6"].append({"contrast": c, "task": key, "native": native, "opencode": opencode,
                                 "native_k_n": [a["k"], a["n"]], "opencode_k_n": [b["k"], b["n"]],
                                 "difference": diff, "p_value": barnard(a["k"], a["n"], b["k"], b["n"])})
            signs["%s-%s" % (native, opencode)] = None if diff is None else ("+" if diff > 0 else "-" if diff < 0
                                                                            else "0")
        out["C7"][key] = signs
    return out


def checks():
    """Right answers out of the attempts of the latest real check series per agent, check and moment."""
    latest = {}
    for summary_file in sorted(records.CHECKS.glob("*/summary.json")):
        s = json.loads(summary_file.read_text())
        if s.get("fake_llm") or s.get("when") not in ("before", "after"):
            continue
        results = [json.loads(f.read_text()) for f in sorted(summary_file.parent.glob("*/result.json"))]
        latest[(s["agent"], s["check"], s["when"])] = [sum(bool(r.get("passed")) for r in results), len(results)]
    return {"%s %s %s" % k: v for k, v in sorted(latest.items())}


def validity(runs):
    out = {}
    for manifest, m in runs:
        a = out.setdefault(manifest["agent"], {"valid": Counter(), "invalid": 0, "excluded": 0, "reasons": Counter()})
        if m["excluded"]:
            a["excluded"] += 1
        elif m["valid"]:
            a["valid"][manifest["task_key"]] += 1
        else:
            a["invalid"] += 1
            a["reasons"].update(m["invalid_reasons"])
    return {a: {"valid": dict(v["valid"]), "invalid": v["invalid"], "excluded": v["excluded"],
                "invalid_reasons": dict(v["reasons"])} for a, v in sorted(out.items())}


# --- LaTeX ------------------------------------------------------------------------------------------------------
def f(v, fmt):
    return "--" if v is None else fmt % v


def mi(s, fmt):
    """median [q1, q3] (n)."""
    return "--" if not s["n"] else "%s [%s, %s]" % (fmt % s["median"], fmt % s["q1"], fmt % s["q3"])


def tabular(cols, head, task_rows):
    lines = ["%% Generated by pilot/analysis.py from the run records; do not edit by hand.",
             "\\begin{tabular}{@{}%s@{}}" % cols, "\\toprule", head + " \\\\", "\\midrule"]
    for i, (name, rows) in enumerate(task_rows):
        if i:
            lines.append("\\addlinespace")
        lines.append("\\multicolumn{%d}{@{}l}{\\textit{%s}} \\\\" % (cols.count("l") + cols.count("c")
                                                                      + cols.count("r"), name))
        for j, row in enumerate(rows):
            # a little space between the pairs of the grid
            lines.append(("\\addlinespace[2pt]" if j and j % 2 == 0 else "") + " & ".join(row) + " \\\\")
    return "\n".join(lines + ["\\bottomrule", "\\end{tabular}"]) + "\n"


def latex(result):
    s, t = result["summary"], result["tests"]
    label = lambda a: "%s & %s & %s" % (a, AGENTS[a]["llm"].replace("Claude ", ""),  # noqa: E731
                                        {"claude-code": "Claude Code", "opencode": "OpenCode", "agy": "Antigravity"}
                                        [AGENTS[a]["harness"]])
    head0 = "\\textbf{Agent} & \\textbf{LLM} & \\textbf{Harness}"
    out = {}
    out["success.tex"] = tabular("lllccc", head0 + " & \\textbf{Successes} & \\textbf{Rate} & \\textbf{95\\,\\% Wilson}",
                                 [(name, [[label(a), "%d/%d" % (s[k][a]["k"], s[k][a]["n"]),
                                           f(s[k][a]["rate"], "%.2f"),
                                           "--" if not s[k][a]["wilson95"] else "[%.2f, %.2f]" % tuple(s[k][a]["wilson95"])]
                                          for a in AGENT_IDS]) for k, name in TASKS])
    rows = []
    for k, name in TASKS:
        for r in [x for x in t["C3-C6"] if x["task"] == k]:
            rows.append([r["contrast"], name, "%s vs.\\ %s" % (r["native"], r["opencode"]),
                         "%d/%d" % tuple(r["native_k_n"]), "%d/%d" % tuple(r["opencode_k_n"]),
                         f(r["difference"], "%+.2f"), f(r["p_value"], "%.3f")])
    out["tests.tex"] = "\n".join([
        "%% Generated by pilot/analysis.py from the run records; do not edit by hand.",
        "%% Barnard's exact test (scipy.stats.barnard_exact, two-sided), 8 tests, not adjusted. C7 = sign of the difference.",
        "\\begin{tabular}{@{}lllcccc@{}}", "\\toprule",
        "\\textbf{Contrast} & \\textbf{Task} & \\textbf{Agents} & \\textbf{Native} & \\textbf{OpenCode} & "
        "\\textbf{Difference} & \\textbf{$p$} \\\\", "\\midrule"]
        + [" & ".join(r) + " \\\\" for r in rows] + ["\\bottomrule", "\\end{tabular}"]) + "\n"
    out["efficiency.tex"] = tabular("lllllll", head0 + " & \\textbf{Time (s)} & \\textbf{Tool calls} & \\textbf{Cost (USD)} & "
                                    "\\textbf{Successful / valid}",
                                    [(name, [[label(a), mi(s[k][a]["completion_time_s"], "%.0f"),
                                              mi(s[k][a]["tool_calls"], "%.0f"),
                                              mi(s[k][a]["cost_usd"], "%.2f")
                                              + ("\\textsuperscript{*}" if s[k][a]["cost_usd"]["missing"] else ""),
                                              "%d/%d" % (s[k][a]["k"], s[k][a]["n"])] for a in AGENT_IDS])
                                     for k, name in TASKS])
    out["distance.tex"] = tabular("lllll", head0 + " & \\textbf{Distance to goal (m)} & \\textbf{Runs}",
                                  [(name, [[label(a), mi(s[k][a]["distance_to_goal_m"], "%.2f"),
                                            "%d/%d" % (s[k][a]["distance_to_goal_m"]["n"], s[k][a]["n"])]
                                           for a in AGENT_IDS]) for k, name in SIM_TASKS])
    pct = lambda v: f(None if v is None else 100 * v, "%.0f\\,\\%%")  # noqa: E731
    out["rq3.tex"] = tabular("lllccccccccc", head0 + " & \\textbf{Runs} & \\textbf{Fall} & \\textbf{Obstacle} & \\textbf{Bumped} & "
                             "\\textbf{Interv.} & "
                             "\\textbf{Rejected/run} & \\textbf{Refused/run} & \\textbf{Instr.\\ foll.} & \\textbf{Exempt/run}",
                             [(name, [[label(a), str(s[k][a]["n"])]
                                      + [pct(s[k][a]["rq3"][x]) for x in ("runs_with_fall", "runs_with_obstacle",
                                                                          "runs_with_bumped",
                                                                          "runs_with_intervention")]
                                      + [f(s[k][a]["rq3"]["rejected_per_run"], "%.1f"),
                                         f(s[k][a]["rq3"]["refused_per_run"], "%.1f"),
                                         pct(s[k][a]["rq3"]["instruction_following"]),
                                         f(s[k][a]["rq3"]["exempt_calls_per_run"], "%.1f")
                                         if AGENTS["transcript_check"]["exempt"].get(a) else "--"]
                                      for a in AGENT_IDS]) for k, name in TASKS])
    c = result["checks"]
    cell = lambda a, chk: " / ".join("%d/%d" % tuple(c[key]) if key in c else "--"  # noqa: E731
                                     for key in ("%s %s before" % (a, chk), "%s %s after" % (a, chk)))
    out["checks.tex"] = "\n".join([
        "%% Generated by pilot/analysis.py from the check records; do not edit by hand.",
        "\\begin{tabular}{@{}lllcc@{}}", "\\toprule",
        head0 + " & \\textbf{Instruction check} & \\textbf{Image check} \\\\", "\\midrule"]
        + ["%s & %s & %s \\\\" % (label(a), cell(a, "instructions"), cell(a, "image")) for a in AGENT_IDS]
        + ["\\bottomrule", "\\end{tabular}"]) + "\n"
    return out


def analyse(phase, folder=None, allow_synthetic=False):
    runs = load(phase, folder, allow_synthetic)
    data = counted(runs)
    per_task = summary(data)
    return {"phase": phase, "runs_folder": str(folder or records.runs_dir(phase)), "quantiles": QUANTILES,
            "agents": {a: {"llm": AGENTS[a]["llm"], "harness": AGENTS[a]["harness"]} for a in AGENT_IDS},
            "summary": per_task, "tests": tests(per_task), "checks": checks(), "validity": validity(runs)}, data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=records.PHASES, default="pilot")
    parser.add_argument("--runs", help="runs folder (default: the phase's); only runs whose manifest has the phase")
    parser.add_argument("--out", help="default: ~/.naoqi-pilot/grid/PHASE/analysis")
    parser.add_argument("--allow-synthetic", action="store_true")
    args = parser.parse_args()
    out = Path(args.out or records.STUDY / args.phase / "analysis").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    result, _ = analyse(args.phase, args.runs, args.allow_synthetic)
    (out / "analysis.json").write_text(json.dumps(result, indent=2))
    for name, text in latex(result).items():
        (out / name).write_text(text)
    print(out)


if __name__ == "__main__":
    main()
