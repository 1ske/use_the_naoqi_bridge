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
    run (the cap-th call; calls received before t0 + the wall-clock cap), or all logged calls for other ends, plus
    the calls the harness itself rejected for invalid arguments before they reached the MCP server (from the
    transcript, before the first rest). Rejected (invalid arguments, at the harness or the bridge) and refused (a
    safety rule or busy) also counted separately, and the bridge's calls per tool (by_tool; descriptive only).
  * cost_usd: from the harness's own report (pilot/config/agents.json, cost_sources); None when it is missing.
  * tokens (descriptive, an addition beyond Table 5.4, 2026-10-19): the harness's token counts (harness_tokens);
    None when missing.
  * safety_events: falls, and obstacle and bumped outcomes, in the same window as tool_calls. Interventions apply
    to Search only. Search falls: the operator's count when given (operator_notes.json falls_observed; NAOqi reported
    a fall the operator did not see, 2026-10-19), else NAOqi's fall events; falls_reported_by_naoqi keeps the latter.
  * instruction_following: whether the first two tool calls are get_state and then stand.
  * distance_to_goal_m (an addition beyond Table 5.4, approved 2026-10-06): the torso's distance to the goal surface
    at the moment rest arrived, from the same ground truth; None if the run did not end with rest or the torso was
    not in the last leg or the corner before it (pilot/success.py, distance_to_goal).
  * valid / invalid_reasons: the runner's reason, if any, a harness that ended with an error before its first model
    turn (agy, Claude Code; OpenCode not handled yet), and a real-time factor below the minimum (protocol.json).
  * provider_failure: in a run that did not end with rest, the error with which the harness gave up after its own
    retries, as {"kind", "message"}: kind "usage_limit" (Claude Code: the Claude subscription's limit; agy: the
    Google account's quota, HTTP 429 RESOURCE_EXHAUSTED) or "provider_error" (any other error from the LLM's service,
    including OpenRouter's). Whether each kind makes the run invalid is protocol.json's invalid_if (section 5.1.3,
    still DECIDE); the reasons stay separate in invalid_reasons. Formats checked against the stand-in LLM
    (pilot/tools/fake_routes/error_*.json), 2026-10-15.
Aggregation over runs (success rates, medians over successful runs) is not done here.
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import success  # noqa: E402

PROTOCOL = json.loads((REPO / "pilot" / "config" / "protocol.json").read_text())
EXCLUDED = json.loads((REPO / "pilot" / "config" / "run_exclusions.json").read_text())["runs"]


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def harness_cost(harness, agent_dir, price=None):
    """The run's cost as the harness reports it (or its token counts at the manifest's list price), and where it
    came from."""
    if harness == "claude-code":
        results = [e for e in read_jsonl(agent_dir / "transcript.jsonl") if e.get("type") == "result"]
        if results and results[-1].get("total_cost_usd") is not None:
            return results[-1]["total_cost_usd"], "Claude Code result event, total_cost_usd"
        return None, "missing: no result event with total_cost_usd in the transcript"
    if harness == "opencode":
        cost_file = agent_dir.parent / "cost.json"
        if not cost_file.exists():
            return None, "missing: no cost.json (OpenRouter usage not read)"
        cost = json.loads(cost_file.read_text())
        if not cost["settled"]:
            if cost.get("error"):
                return None, "missing: the settling stopped with an error (%s); run.py resettle" % cost["error"]
            if cost.get("waited_s") is None:  # cost.json as written at the start of the run (run.py write_cost_pending)
                return None, "missing for now: OpenRouter's usage is still settling (or the settling stopped; " \
                             "run.py resettle)"
            return None, "missing: OpenRouter's usage did not settle within %d s" % cost["waited_s"]
        return cost["cost_usd"], cost.get("source", "OpenRouter key usage, after minus before the run")
    if harness == "agy":
        results = [e["result"] for e in read_jsonl_lenient(agent_dir / "transcript.jsonl") if e.get("event") == "result"]
        if not results or not results[-1].get("usage"):
            return None, "missing: no result event with token counts in agy's transcript"
        if not price:
            return None, "missing: no list price in the manifest"
        usage, rate = results[-1]["usage"], price["usd_per_million_tokens"]
        cost = (usage.get("input_tokens", 0) * rate["input"] + usage.get("cache_read_tokens", 0) * rate["cached_input"]
                + usage.get("output_tokens", 0) * rate["output_including_thinking"]) / 1e6
        return round(cost, 8), "agy token counts at the %s list price (%s, accessed %s)" % (
            price["model"], price["tier"], price["accessed"])
    return None, "missing: cost collection for %s is not implemented yet" % harness


def harness_tokens(harness, agent_dir):
    """The run's token counts as the harness reports them, in one shape: input (not from the cache), cache_read,
    cache_write (None: not reported), output (including reasoning), reasoning, and total (their sum). Checked against
    the pilot records, 2026-10-19: OpenCode's output leaves out reasoning (output + reasoning at the catalogue price
    gives OpenRouter's step cost), Claude Code's output_tokens includes thinking (output_tokens_details), agy's
    output_tokens includes thinking (in every step thinking < output and total = input + output). OpenCode's title
    request is not in its transcript, so not counted."""
    if harness == "claude-code":
        results = [e for e in read_jsonl(agent_dir / "transcript.jsonl") if e.get("type") == "result"]
        if not results or not results[-1].get("usage"):
            return None, "missing: no result event with usage in the transcript"
        u = results[-1]["usage"]
        counts = {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
                  "cache_write": u.get("cache_creation_input_tokens", 0), "output": u.get("output_tokens", 0),
                  "reasoning": (u.get("output_tokens_details") or {}).get("thinking_tokens")}
        source = "Claude Code result event, usage"
    elif harness == "opencode":
        steps = [(e.get("part") or {}).get("tokens") or {} for e in read_jsonl_lenient(agent_dir / "transcript.jsonl")
                 if e.get("type") == "step_finish"]
        if not steps:
            return None, "missing: no step_finish events in OpenCode's transcript"
        counts = {"input": sum(t.get("input", 0) for t in steps),
                  "cache_read": sum((t.get("cache") or {}).get("read", 0) for t in steps),
                  "cache_write": sum((t.get("cache") or {}).get("write", 0) for t in steps),
                  "output": sum(t.get("output", 0) + t.get("reasoning", 0) for t in steps),
                  "reasoning": sum(t.get("reasoning", 0) for t in steps)}
        source = "OpenCode step_finish events, summed (title request not included)"
    elif harness == "agy":
        results = [e["result"] for e in read_jsonl_lenient(agent_dir / "transcript.jsonl") if e.get("event") == "result"]
        if not results or not results[-1].get("usage"):
            return None, "missing: no result event with token counts in agy's transcript"
        u = results[-1]["usage"]
        counts = {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_tokens", 0), "cache_write": None,
                  "output": u.get("output_tokens", 0), "reasoning": u.get("thinking_tokens")}
        source = "agy result event, usage"
    else:
        return None, "missing: token collection for %s is not implemented yet" % harness
    counts["total"] = sum(counts[k] or 0 for k in ("input", "cache_read", "cache_write", "output"))
    return counts, source


def harness_rejected(harness, agent_dir):
    """NAO tool calls the harness rejected for invalid arguments (they never reach the MCP server), in transcript
    order, cut at the first rest call. None when this harness's transcript format is not handled."""
    events = read_jsonl_lenient(agent_dir / "transcript.jsonl")
    calls = []  # (tool, rejected) in transcript order
    if harness == "opencode":
        for e in events:
            part = e.get("part") or {}
            if e.get("type") == "tool_use" and part.get("type") == "tool":
                state = part.get("state") or {}
                calls.append((part.get("tool"), state.get("status") == "error"
                              and str(state.get("error", "")).startswith("Invalid arguments")))
    elif harness == "claude-code":
        errors = {}
        for e in events:
            for block in ((e.get("message") or {}).get("content") or []) if e.get("type") == "user" else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    errors[block.get("tool_use_id")] = json.dumps(block.get("content"))
        for e in events:
            for block in ((e.get("message") or {}).get("content") or []) if e.get("type") == "assistant" else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append((block.get("name"), "InputValidationError" in errors.get(block.get("id"), "")))
    elif harness == "agy":
        # agy's own log (copied by the runner): each model turn lists its calls; a call agy rejects is followed by
        # a step with status INVALID and an "invalid arguments" error.
        for path in sorted(agent_dir.glob("agy-brain/*/.system_generated/logs/transcript_full.jsonl")):
            for e in read_jsonl_lenient(path):
                if e.get("type") == "PLANNER_RESPONSE":
                    calls += [(c.get("name"), False) for c in e.get("tool_calls") or []]
                elif e.get("status") == "INVALID" and str(e.get("error", "")).startswith("invalid arguments") and calls:
                    calls[-1] = (calls[-1][0], True)
    else:
        return None
    prefix = {"opencode": "naoqi_", "claude-code": "mcp__naoqi__", "agy": "mcp_naoqi_"}[harness]
    rejected = 0
    for tool, was_rejected in calls:
        if not (tool or "").startswith(prefix):
            continue  # only NAO tool calls count; other calls are the transcript check's
        rejected += was_rejected
        if tool and tool.endswith("rest") and not was_rejected:
            break
    return rejected


def read_jsonl_lenient(path):
    records = []
    for line in path.read_text(errors="replace").splitlines() if path.exists() else []:
        try:
            records.append(json.loads(line))
        except ValueError:
            pass  # e.g. a last line cut off when the container was stopped
    return records


def startup_error(harness, agent_dir):
    """The harness's error if it ended with an error before its first model turn (login, eligibility, provider):
    a failure outside the agent, so the run is invalid (section 5.1.3). None otherwise or when not handled."""
    events = read_jsonl_lenient(agent_dir / "transcript.jsonl")
    if harness == "agy":
        results = [e["result"] for e in events if e.get("event") == "result"]
        if results and results[-1].get("status") == "ERROR" and not results[-1].get("num_turns"):
            return results[-1].get("error") or "agy ended with an error"
    elif harness == "claude-code":
        results = [e for e in events if e.get("type") == "result"]
        if results and results[-1].get("is_error") and not any(e.get("type") == "assistant" for e in events):
            return results[-1].get("result") or results[-1].get("subtype") or "Claude Code ended with an error"
    return None


def provider_failure(harness, agent_dir):
    """The error from the LLM's service with which the harness ended, after its retries: {"kind", "message"} or None."""
    events = read_jsonl_lenient(agent_dir / "transcript.jsonl")
    if harness == "claude-code":
        # Claude Code ends with an assistant message carrying an error type, and a result with is_error.
        errors = [e for e in events if e.get("type") == "assistant" and e.get("error")]
        if not errors or errors[-1]["error"] == "authentication_failed":
            return None  # a failed login is the startup check's
        last = errors[-1]
        text = " ".join(b.get("text", "") for b in (last.get("message") or {}).get("content") or []
                        if isinstance(b, dict) and b.get("type") == "text")
        limit = last["error"] == "rate_limit" and re.search(r"hit your .*limit", text, re.I)
        return {"kind": "usage_limit" if limit else "provider_error", "message": "%s: %s" % (last["error"], text)}
    if harness == "opencode":
        errors = [e for e in events if e.get("type") == "error"]
        if errors:
            err = errors[-1].get("error") or {}
            return {"kind": "provider_error", "message": "%s %s: %s" % (err.get("type"), err.get("status"),
                                                                       err.get("message"))}
    if harness == "agy":
        results = [e["result"] for e in events if e.get("event") == "result"]
        if results and results[-1].get("status") == "ERROR" and results[-1].get("num_turns"):
            error = str(results[-1].get("error") or "")
            if "RESOURCE_EXHAUSTED" in error or "Error 429" in error:
                return {"kind": "usage_limit", "message": error}
            if re.search(r"generating and executing: Error \d+", error):
                return {"kind": "provider_error", "message": error}
    return None


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
    check_file = run_dir / "transcript_check.json"  # missing when the runner was stopped before collecting
    check = json.loads(check_file.read_text()) if check_file.exists() else {"protocol_violation": False,
                                                                             "transcript_found": False}
    search = manifest["task_key"] == "search"
    notes_file = run_dir / "operator_notes.json"  # Search: the operator's interventions and verdict (pilot/search.py)
    notes = json.loads(notes_file.read_text()) if notes_file.exists() else {}
    if search:
        verdict = notes.get("verdict") if normal_end else None
        if verdict:  # recomputed from the recorded facts under the current criteria (protocol.json, search)
            verdict = dict(verdict, success=bool(verdict["within_distance"] and (
                verdict["ball_visible_in_rest_image"] or not PROTOCOL["search"]["require_ball_visible_in_rest_image"])))
    else:
        verdict = success.verdict(manifest["task_key"], ground_truth) if normal_end else None
    succeeded = bool(verdict and verdict["success"]) and not check["protocol_violation"]

    invalid = [manifest["invalid_reason"]] if manifest.get("invalid_reason") else []
    if not check_file.exists() and not invalid:
        invalid.append("no transcript check: the run was not collected")
    error = startup_error(manifest["agent_config"]["harness"], run_dir / "agent")
    if error:
        invalid.append("the harness ended with an error before its first model turn: %s" % error)
    failure = provider_failure(manifest["agent_config"]["harness"], run_dir / "agent") \
        if manifest["end_condition"] != "stop_signal" else None
    if failure and PROTOCOL["invalid_if"][failure["kind"]]:
        invalid.append({"usage_limit": "usage limit reached: %s",
                        "provider_error": "provider error after the harness's retries: %s"}[failure["kind"]]
                       % failure["message"])
    rtf, rtf_min = ground_truth.get("real_time_factor"), PROTOCOL["min_real_time_factor"]["value"]
    if search:
        if notes.get("invalid_reason"):
            invalid.append("operator: %s" % notes["invalid_reason"])
        if normal_end and not invalid and not notes.get("verdict"):
            invalid.append("verdict pending" if notes.get("verdict_pending") else "no verdict recorded by the operator")
    elif rtf is None:
        invalid.append("no real-time factor recorded")
    elif rtf < rtf_min:
        invalid.append("real-time factor %.3f below %.2f" % (rtf, rtf_min))

    outcomes = [(c.get("result") or {}).get("result") for c in calls if c["outcome"] == "ok"]
    cost, cost_source = harness_cost(manifest["agent_config"]["harness"], run_dir / "agent", manifest.get("price"))
    tokens, tokens_source = harness_tokens(manifest["agent_config"]["harness"], run_dir / "agent")
    at_harness = harness_rejected(manifest["agent_config"]["harness"], run_dir / "agent")
    return {
        "run_id": manifest["run_id"], "agent": manifest["agent"], "task": manifest["task"],
        "task_key": manifest["task_key"], "phase": manifest.get("phase"),
        "test_run": bool(manifest.get("test")) or manifest.get("phase") == "test",
        "excluded": EXCLUDED.get(manifest["run_id"]),  # a reason when the run is not pilot or main-run data
        "valid": not invalid, "invalid_reasons": invalid, "provider_failure": failure,
        "end_condition": manifest["end_condition"],
        "task_success": int(succeeded),
        "success_check": {
            "ended_with_rest": normal_end,
            "protocol_violation": check["protocol_violation"],
            "operator_verdict": verdict,
            "reason": ("operator: within %s m: %s, ball visible: %s" % (
                verdict["criteria"]["max_distance_m"], verdict["within_distance"], verdict["ball_visible_in_rest_image"])
                if verdict else "the run did not end with rest or has no verdict"),
        } if search else {
            "ended_with_rest": normal_end,
            "upright": verdict.get("upright") if verdict else None,
            "inside_area": verdict.get("inside_area") if verdict else None,
            "torso_xy_m": verdict.get("torso_xy") if verdict else None,
            "area": verdict["area"] if verdict else success.area(manifest["task_key"]),
            "protocol_violation": check["protocol_violation"],
            "reason": verdict["reason"] if verdict else "the run did not end with rest",
        },
        "distance_to_goal_m": None if search else success.distance_to_goal(manifest["task_key"], verdict["torso_xy"])
        if verdict and verdict.get("torso_xy") else None,
        "completion_time_s": round(rests[0]["returned"] - t0, 3) if normal_end and t0 is not None else None,
        "tool_calls": {
            "total": len(calls) + (at_harness or 0),
            "rejected": sum(c["outcome"] == "rejected" for c in calls) + (at_harness or 0),
            "rejected_by_harness": at_harness,  # None: not handled for this harness yet
            "refused": sum(c["outcome"] == "refused" for c in calls),
            # calls that reached the bridge, per tool (harness-rejected calls are only in rejected_by_harness)
            "by_tool": {t: sum(c["tool"] == t for c in calls) for t in sorted({c["tool"] for c in calls})},
        },
        "cost_usd": cost, "cost_source": cost_source,
        "tokens": tokens, "tokens_source": tokens_source,
        "safety_events": {
            # Search: the falls the operator saw, when given (pilot/search.py falls; author 2026-10-19), else NAOqi's
            "falls": (notes.get("falls_observed") or {}).get("count", len(falls)) if search else len(falls),
            "falls_reported_by_naoqi": len(falls),
            "obstacle": outcomes.count("obstacle"),
            "bumped": outcomes.count("bumped"),
            "interventions": len(notes.get("interventions") or []) if search else None,  # Search only
        },
        "instruction_following": [c["tool"] for c in calls[:2]] == ["get_state", "stand"],
        "real_time_factor": rtf,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: metrics.py RUN_DIR [RUN_DIR ...]")
    for path in sys.argv[1:]:
        print(json.dumps(metrics(path), indent=2))
