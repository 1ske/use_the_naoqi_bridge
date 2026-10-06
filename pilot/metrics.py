"""Table 5.4's metrics for one run folder written by pilot/run.py (the same script for the pilot and the main runs).

    python3 pilot/metrics.py RUN_DIR [RUN_DIR ...]     -> one JSON object per run on stdout

Per run:
  * task_success: 1 if the run ended with rest (end condition stop_signal), the robot was upright and its torso in
    the success area at that moment (pilot/success.py, from the supervisor's ground truth), and the transcript check
    found no protocol violation; else 0.
  * completion_time_s: T = t_end - t0, with t0 = the prompt submission (agent/t0, written in the container) and
    t_end = the moment the bridge returned the first rest's result (calls.jsonl). Both come from the Docker VM's
    clock. Only for runs that ended with rest.
  * tool_calls: the calls the bridge received up to and including the first rest, or up to the cap that ended the
    run (the cap-th call; calls received before t0 + the wall-clock cap), or all logged calls for other ends.
    Rejected (invalid arguments, unknown tool) and refused (a safety rule or busy) also counted separately.
  * cost_usd: from the harness's own report (pilot/config/agents.json, cost_sources); None when it is missing.
  * safety_events: falls, and obstacle and bumped outcomes, in the same window as tool_calls. Interventions apply
    to Search only.
  * instruction_following: whether the first two tool calls are get_state and then stand.
  * distance_to_goal_m (an addition beyond Table 5.4, approved 2026-10-06): the torso's distance to the goal surface
    at the moment rest arrived, from the same ground truth; None if the run did not end with rest or the torso was
    not in the last leg or the corner before it (pilot/success.py, distance_to_goal).
  * valid / invalid_reasons: the runner's reason, if any, and a real-time factor below the minimum (protocol.json).
Aggregation over runs (success rates, medians over successful runs) is not done here.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import success  # noqa: E402

PROTOCOL = json.loads((REPO / "pilot" / "config" / "protocol.json").read_text())


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def harness_cost(harness, agent_dir):
    """The run's cost as the harness reports it, and where it came from."""
    if harness == "claude-code":
        results = [e for e in read_jsonl(agent_dir / "transcript.jsonl") if e.get("type") == "result"]
        if results and results[-1].get("total_cost_usd") is not None:
            return results[-1]["total_cost_usd"], "Claude Code result event, total_cost_usd"
        return None, "missing: no result event with total_cost_usd in the transcript"
    return None, "missing: cost collection for %s is not implemented yet" % harness


def metrics(run_dir):
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    log = read_jsonl(run_dir / "calls.jsonl")
    calls = sorted((r for r in log if r["type"] == "call"), key=lambda r: r["received"])
    falls = [r for r in log if r["type"] == "fall"]
    rests = [c for c in calls if c["tool"] == "rest"]
    normal_end = manifest["end_condition"] == "stop_signal" and bool(rests)
    t0_file = run_dir / "agent" / "t0"
    t0 = float(t0_file.read_text()) if t0_file.exists() else None
    # The run's window. The runner closes the log a moment after it sees the end, so a fast agent's next calls
    # can still be in it; they are not part of the run.
    if normal_end:
        calls = [c for c in calls if c["received"] <= rests[0]["received"]]
        falls = [f for f in falls if f["time"] < rests[0]["received"]]
    elif manifest["end_condition"] == "tool_call_cap":
        calls = calls[:manifest["caps"]["tool_calls"]]
        falls = [f for f in falls if f["time"] <= calls[-1]["returned"]]
    elif manifest["end_condition"] == "wall_clock_cap" and t0 is not None:
        end = t0 + manifest["caps"]["wall_clock_s"]
        calls = [c for c in calls if c["received"] < end]
        falls = [f for f in falls if f["time"] < end]

    ground_truth_file = run_dir / "ground_truth.json"
    ground_truth = json.loads(ground_truth_file.read_text()) if ground_truth_file.exists() else {}
    check = json.loads((run_dir / "transcript_check.json").read_text())
    verdict = success.verdict(manifest["task_key"], ground_truth) if normal_end else None
    succeeded = bool(verdict and verdict["success"]) and not check["protocol_violation"]

    invalid = [manifest["invalid_reason"]] if manifest.get("invalid_reason") else []
    rtf, rtf_min = ground_truth.get("real_time_factor"), PROTOCOL["min_real_time_factor"]["value"]
    if rtf is None:
        invalid.append("no real-time factor recorded")
    elif rtf < rtf_min:
        invalid.append("real-time factor %.3f below %.2f" % (rtf, rtf_min))

    outcomes = [(c.get("result") or {}).get("result") for c in calls if c["outcome"] == "ok"]
    cost, cost_source = harness_cost(manifest["agent_config"]["harness"], run_dir / "agent")
    return {
        "run_id": manifest["run_id"], "agent": manifest["agent"], "task": manifest["task"],
        "test_run": bool(manifest.get("test")),
        "valid": not invalid, "invalid_reasons": invalid,
        "end_condition": manifest["end_condition"],
        "task_success": int(succeeded),
        "success_check": {
            "ended_with_rest": normal_end,
            "upright": verdict.get("upright") if verdict else None,
            "inside_area": verdict.get("inside_area") if verdict else None,
            "torso_xy_m": verdict.get("torso_xy") if verdict else None,
            "area": verdict["area"] if verdict else success.area(manifest["task_key"]),
            "protocol_violation": check["protocol_violation"],
            "reason": verdict["reason"] if verdict else "the run did not end with rest",
        },
        "distance_to_goal_m": success.distance_to_goal(manifest["task_key"], verdict["torso_xy"])
        if verdict and verdict.get("torso_xy") else None,
        "completion_time_s": round(rests[0]["returned"] - t0, 3) if normal_end and t0 is not None else None,
        "tool_calls": {
            "total": len(calls),
            "rejected": sum(c["outcome"] == "rejected" for c in calls),
            "refused": sum(c["outcome"] == "refused" for c in calls),
        },
        "cost_usd": cost, "cost_source": cost_source,
        "safety_events": {
            "falls": len(falls),
            "obstacle": outcomes.count("obstacle"),
            "bumped": outcomes.count("bumped"),
            "interventions": None,  # Search only
        },
        "instruction_following": [c["tool"] for c in calls[:2]] == ["get_state", "stand"],
        "real_time_factor": rtf,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: metrics.py RUN_DIR [RUN_DIR ...]")
    for path in sys.argv[1:]:
        print(json.dumps(metrics(path), indent=2))
