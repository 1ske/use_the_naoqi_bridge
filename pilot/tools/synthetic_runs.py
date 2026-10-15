"""Synthetic run records for testing the analysis and the figures (never data; never in ~/.naoqi-pilot/grid).

    python3 pilot/tools/synthetic_runs.py OUT [--runs 10] [--seed 1] [--phase pilot]

Writes OUT/<run_id>/ in the runner's format (manifest.json with "synthetic": true, calls.jsonl, ground_truth.json,
trajectory.jsonl, transcript_check.json, agent/t0, agent/transcript.jsonl, cost.json for OpenCode), for every agent
and simulated task, so that pilot/metrics.py, pilot/analysis.py --allow-synthetic and pilot/figures.py
--allow-synthetic can be run on them. Success probabilities and costs are made up per agent; a few runs are invalid
(low real-time factor, provider error), some fall, some end at a cap.
"""
import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "pilot"))
import success  # noqa: E402
from agent_container import AGENT_IDS, AGENTS  # noqa: E402

P_SUCCESS = {"A1": 0.9, "A2": 0.7, "A3": 0.75, "A4": 0.6, "A5": 0.4, "A6": 0.35, "A7": 0.5, "A8": 0.55}
COST = {"A1": 1.2, "A2": 1.0, "A3": 0.6, "A4": 0.5, "A5": 0.05, "A6": 0.04, "A7": 0.08, "A8": 0.07}
PRICE = json.loads((REPO / "pilot" / "config" / "prices.json").read_text())["gemini-3.8-flash"]
PREFIX = {"claude-code": "mcp__naoqi__", "opencode": "naoqi_", "agy": "mcp_naoqi_"}


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def one(out, phase, task, agent, i, rng, t_start):
    spec = AGENTS[agent]
    harness = spec["harness"]
    area = success.area(task)
    run_id = "%s-%s-synthetic%03d" % (task, agent, i)
    d = out / run_id
    (d / "agent").mkdir(parents=True)
    t0 = t_start
    roll = rng.random()
    invalid_rtf = roll < 0.04
    provider_error = 0.04 <= roll < 0.07
    ok = not provider_error and rng.random() < P_SUCCESS[agent]
    end = "stop_signal" if ok or rng.random() < 0.6 else rng.choice(["wall_clock_cap", "session_ended"])
    if provider_error:
        end = "session_ended"
    fell = not ok and rng.random() < 0.15
    n_go = rng.randint(3, 12)
    tools = (["get_state", "stand"] if rng.random() < 0.85 else ["stand", "get_state"]) \
        + [rng.choice(["go", "go", "walk_to", "look_around", "look"]) for _ in range(n_go)]
    if end == "stop_signal":
        tools.append("rest")
    calls, t = [], t0 + 3
    for tool in tools:
        dur = {"go": rng.uniform(8, 25), "walk_to": rng.uniform(3, 8), "look_around": 9}.get(tool, 1.0)
        outcome = "refused" if rng.random() < 0.03 else "rejected" if rng.random() < 0.03 else "ok"
        result = {"result": "obstacle" if tool == "go" and rng.random() < 0.25 else
                  "bumped" if tool == "go" and rng.random() < 0.05 else "arrived"} if outcome == "ok" else None
        calls.append({"type": "call", "tool": tool, "arguments": {}, "received": t, "returned": t + dur,
                      "outcome": outcome, "result": result})
        t += dur + rng.uniform(2, 8)
    if fell:
        calls.insert(len(calls) // 2, {"type": "fall", "time": calls[len(calls) // 2]["received"] + 1, "cause": "fell"})
    write_jsonl(d / "calls.jsonl", calls)
    (d / "agent" / "t0").write_text("%.6f\n" % t0)
    gx = (area["x"][0] + area["x"][1]) / 2
    if ok:
        torso = [gx + rng.uniform(-0.4, 0.4), area["goal_surface_y"] - rng.uniform(0.45, 0.95)]
    else:
        torso = [gx + rng.uniform(-0.4, 0.4), area["goal_surface_y"] - rng.uniform(1.1, 2.4)]
    gt = {"run_id": run_id, "world": Path(success.CONFIG["worlds"][task]["file"]).name, "real_time_mode": True,
          "real_time_factor": 0.85 if invalid_rtf else round(rng.uniform(0.91, 0.99), 4)}
    if end == "stop_signal":
        gt["rest"] = {"torso": torso + [0.31], "tilt_deg": 85.0 if fell and rng.random() < 0.5 else 0.5,
                      "upright": True}
        gt["rest"]["upright"] = gt["rest"]["tilt_deg"] < 45
    (d / "ground_truth.json").write_text(json.dumps(gt))
    start = success.boxes(success.CONFIG["worlds"][task]["file"])
    path = [(0.4 + k * 0.2, 0.4) for k in range(8)] + [(torso[0], torso[1])]
    write_jsonl(d / "trajectory.jsonl", [{"mac_t": t0 + k, "x": x, "y": y, "heading_deg": 0, "tilt_deg": 0}
                                         for k, (x, y) in enumerate(path)])
    exempt = {n: 0 for n in AGENTS["transcript_check"]["exempt"].get(agent, [])}
    if exempt:
        exempt["view_file"] = rng.randint(2, 10)
    (d / "transcript_check.json").write_text(json.dumps({
        "agent": agent, "harness": harness, "transcript_found": True, "protocol_violation": False,
        "nao_tool_calls": len(tools), "exempt_calls": exempt, "violating_calls": {}}))
    cost = round(COST[agent] * rng.uniform(0.5, 1.6) * (len(tools) / 8), 4)
    events = []
    if harness == "claude-code":
        if provider_error:
            events.append({"type": "assistant", "error": "server_error",
                           "message": {"content": [{"type": "text", "text": "API Error: 500 (synthetic)"}]}})
        events.append({"type": "result", "subtype": "success", "is_error": provider_error, "total_cost_usd": cost})
    elif harness == "opencode":
        if provider_error:
            events.append({"type": "error", "error": {"type": "provider.internal", "status": 500,
                                                      "message": "Provider returned error (synthetic)"}})
        (d / "cost.json").write_text(json.dumps({"cost_usd": cost, "settled": True, "waited_s": 130}))
    else:
        tok = int(cost / PRICE["usd_per_million_tokens"]["input"] * 1e6 * 0.8)
        events.append({"event": "result", "result": {
            "status": "ERROR" if provider_error else "DONE", "num_turns": len(tools),
            "error": "agent executor error: generating and executing: Error 500 (synthetic)" if provider_error else None,
            "usage": {"input_tokens": tok, "cache_read_tokens": tok // 4, "output_tokens": tok // 50}}})
    write_jsonl(d / "agent" / "transcript.jsonl", events)
    manifest = {"run_id": run_id, "synthetic": True, "phase": phase, "task": success.CONFIG["worlds"][task]["task"],
                "task_key": task, "agent": agent, "run_order": {"position": i, "block": i, "attempt": 1},
                "agent_config": spec, "caps": {"wall_clock_s": 1800, "tool_calls": 100},
                "end_condition": end, "invalid_reason": None, "test": None}
    if harness == "agy":
        manifest["price"] = dict(PRICE, model="gemini-3.8-flash")
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return start


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out")
    parser.add_argument("--runs", type=int, default=10, help="per agent and task")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--phase", default="pilot")
    args = parser.parse_args()
    out = Path(args.out).expanduser().resolve()
    if str(out).startswith(str(Path.home() / ".naoqi-pilot")):
        raise SystemExit("synthetic records never go under ~/.naoqi-pilot")
    out.mkdir(parents=True, exist_ok=False)
    rng = random.Random(args.seed)
    t = 1_800_000_000.0
    for task in ("corridor", "corridor_complex"):
        for i in range(1, args.runs + 1):
            for agent in AGENT_IDS:
                one(out, args.phase, task, agent, i, rng, t)
                t += 600
    print(out)


if __name__ == "__main__":
    main()
