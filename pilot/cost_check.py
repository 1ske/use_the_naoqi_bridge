"""Reconcile each OpenCode agent's OpenRouter key with the costs in the records (reads the records, changes nothing).

    python3 pilot/cost_check.py [--phase main] [--agents A2,A4,A6,A8] [--tolerance 0.001]

Run it at least a day after the phase's last run (OpenRouter posted some usage hours late in the pilots, 2026-10-15).
A key's usage only grows, so every record that read it (runs of any phase, check attempts, check series) is a window
[usage before, usage after], and the windows sorted by their usage before form a chain from the phase's first run on:
  * gap: usage between one window's after and the next window's before, which no record holds: usage that posted
    after a window settled (its cost is too low), or a use of the key outside the runner. Should be 0.
  * tail: the key's usage now minus the last window's after. Should be 0.
  * a window whose cost did not settle (missing): its rest falls into the next gap or window, so the chain is only
    exact up to it; resettle it (run.py resettle) or report it as missing.
  * cost / step costs outside RATIO: OpenRouter's usage for the run against the costs OpenRouter returned to OpenCode
    per request (title request not included; 0.77-1.20 for settled pilot runs). A run whose cost settled too early is
    low, and the next run of the agent then high.
Exits 1 when anything is flagged.
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import records  # noqa: E402
import run as runner  # noqa: E402
from agent_container import AGENT_IDS, AGENTS  # noqa: E402

RATIO = (0.7, 1.4)


def windows(agent):
    """Every record of the agent's own key with a usage before: [{label, before, after, cost, settled, steps}]."""
    out = []
    for cost_file in records.STUDY.glob("*/runs/*/cost.json"):
        manifest = json.loads((cost_file.parent / "manifest.json").read_text())
        cost = json.loads(cost_file.read_text())
        if manifest.get("agent") != agent or "shared key" in cost.get("source", ""):
            continue  # the shared key (A2, A4, A6, A8) before 2026-10-15 is another key
        out.append(dict(label="%s/%s" % (manifest.get("phase"), cost_file.parent.name), **fields(cost)))
    for cost_file in records.CHECKS.glob("%s-*/*/cost.json" % agent):  # attempts settled one by one
        out.append(dict(label="checks/" + cost_file.parent.name, **fields(json.loads(cost_file.read_text()))))
    for series in records.CHECKS.glob("series-cost-*.json"):  # one window per key over a series of attempts
        cost = json.loads(series.read_text())
        if cost.get("agent") == agent:
            out.append(dict(label="checks/" + series.name, **fields(cost)))
    return sorted((w for w in out if w["before"] is not None), key=lambda w: w["before"])


def fields(cost):
    return {"before": cost.get("usage_before_usd"), "after": cost.get("usage_after_usd") if cost.get("settled") else None,
            "cost": cost.get("cost_usd") if cost.get("settled") else None, "settled": bool(cost.get("settled")),
            "steps": cost.get("cross_check_step_costs_usd")}


def check(agent, phase, tolerance):
    """Prints the agent's chain; returns the number of flagged items."""
    chain = windows(agent)
    first = next((w for w in chain if w["label"].startswith(phase + "/")), None)
    if first is None:
        print("%s: no %s run with an OpenRouter cost" % (agent, phase))
        return 0
    chain = chain[chain.index(first):]
    now = runner.openrouter_usage(runner.openrouter_key(agent))
    flags = 0
    print("%s: key usage %.6f at the first %s run, %.6f now (%d windows)" % (agent, first["before"], phase, now, len(chain)))
    for w, nxt in zip(chain, chain[1:] + [None]):
        notes = []
        if not w["settled"]:
            notes.append("MISSING: did not settle")
        elif w["steps"]:
            ratio = w["cost"] / w["steps"]
            if not RATIO[0] <= ratio <= RATIO[1]:
                notes.append("cost / step costs %.2f" % ratio)
        print("  %-62s cost %-11s %s" % (w["label"], "%.6f" % w["cost"] if w["cost"] is not None else "-",
                                         "; ".join(notes)))
        flags += bool(notes)
        end = nxt["before"] if nxt else now
        if w["after"] is not None and end - w["after"] > tolerance:
            print("  %-62s GAP %.6f %s" % ("", end - w["after"], "(tail: posted after the last window)" if not nxt
                                           else "(no record holds it)"))
            flags += 1
    recorded = sum(w["cost"] for w in chain if w["cost"] is not None)
    print("  usage since the first %s run %.6f, recorded %.6f, not in any record %.6f, windows missing %d\n" % (
        phase, now - first["before"], recorded, now - first["before"] - recorded, sum(not w["settled"] for w in chain)))
    return flags


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=[p for p in records.PHASES if p != "test"], default="main")
    parser.add_argument("--agents", default=",".join(a for a in AGENT_IDS if AGENTS[a]["harness"] == "opencode"))
    parser.add_argument("--tolerance", type=float, default=0.001, help="USD")
    args = parser.parse_args()
    flags = sum(check(a, args.phase, args.tolerance) for a in args.agents.split(","))
    print("flagged: %d" % flags)
    sys.exit(1 if flags else 0)


if __name__ == "__main__":
    main()
