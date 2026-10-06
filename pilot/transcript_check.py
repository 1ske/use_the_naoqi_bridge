"""Transcript check, applied after every run (rule fixed 2026-10-05, in pilot/config/agents.json).

A call to any tool other than the 8 NAO tools is a protocol violation, and the run counts as a failure.
Exception, A6 only: manage_task, list_resources and read_resource, and view_file on an image agy offloaded from a
tool result, are not violations; they are counted (rule changed 2026-10-06, see agents.json).
Every call the transcript records counts, also one that the harness refused or that failed.

Sources: Claude Code's and OpenCode's JSON transcripts (transcript.jsonl). For agy, its own conversation logs
(agy-brain/<conversation>/.system_generated/logs/transcript_full.jsonl, copied from the container by the runner),
because agy's stream-json output records some tool calls only as a step of type "unknown" without the tool name.
Every model turn there lists the tool calls the model made, including ones agy then rejected.

    python3 pilot/transcript_check.py AGENT RUN_DIR     -> JSON summary on stdout
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "config" / "agents.json"
VIEW_FILE_ALLOWED = json.loads(CONFIG.read_text())["transcript_check"]["view_file_allowed_path"]


def tool_calls(harness, lines):
    """Yield the name of every tool call in a harness's JSON-lines transcript, once per call."""
    seen = set()
    for event in lines:
        if harness == "claude-code":
            # stream-json: assistant messages carry tool_use blocks (subagent messages too, if there were any).
            message = event.get("message") if event.get("type") == "assistant" else None
            for block in (message or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") not in seen:
                    seen.add(block.get("id"))
                    yield block.get("name")
        elif harness == "opencode":
            # run --format json: one tool_use event per call, with its call ID.
            part = event.get("part") or {}
            if event.get("type") == "tool_use" and part.get("type") == "tool":
                key = part.get("id") or part.get("partID")
                if key not in seen:
                    seen.add(key)
                    yield part.get("tool")
        elif harness == "agy":
            # agy's conversation log: each model turn (PLANNER_RESPONSE) lists the tool calls it made. view_file is
            # exempt only on an image agy offloaded from a tool result; on any other path it counts as its own name.
            if event.get("type") == "PLANNER_RESPONSE":
                for call in event.get("tool_calls") or []:
                    name, path = call.get("name"), str((call.get("args") or {}).get("AbsolutePath", ""))
                    if name == "view_file" and not re.match(VIEW_FILE_ALLOWED, path):
                        name = "view_file(%s)" % path
                    yield name
        else:
            raise ValueError("unknown harness %r" % harness)


def transcript_files(harness, run_dir):
    run_dir = Path(run_dir)
    if harness != "agy":
        return [run_dir / "transcript.jsonl"]
    # One log per conversation (the agent's own; subagents, if any, would have their own).
    logs = []
    for conversation in sorted((run_dir / "agy-brain").glob("*/.system_generated/logs")):
        full = conversation / "transcript_full.jsonl"
        logs.append(full if full.exists() else conversation / "transcript.jsonl")
    return logs


def check(agent, run_dir):
    run_dir = Path(run_dir)
    config = json.loads(CONFIG.read_text())
    rule = config["transcript_check"]
    harness = config[agent]["harness"]
    nao = {rule["tool_name_format"][harness].format(tool=t): t for t in rule["nao_tools"]}
    exempt = set(rule["exempt"].get(agent, []))

    files = [f for f in transcript_files(harness, run_dir) if f.exists()]
    lines, unreadable = [], 0
    for path in files:
        for raw in path.read_text(errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                lines.append(json.loads(raw))
            except ValueError:
                unreadable += 1  # e.g. a last line cut off when the container was stopped
    nao_calls, exempt_calls, violations = Counter(), Counter(), []
    for name in tool_calls(harness, lines):
        if name in nao:
            nao_calls[nao[name]] += 1
        elif name in exempt:
            exempt_calls[name] += 1
        else:
            violations.append(name)
    return {
        "agent": agent,
        "harness": harness,
        "transcript_files": [str(f.relative_to(run_dir)) for f in files],
        # No transcript means the check could not be made; the runner then marks the run invalid.
        "transcript_found": bool(files),
        "unreadable_lines": unreadable,
        "nao_tool_calls": sum(nao_calls.values()),
        "nao_tool_calls_by_tool": dict(nao_calls),
        "exempt_calls": {name: exempt_calls.get(name, 0) for name in sorted(exempt)},
        "violating_calls": dict(Counter(violations)),
        "protocol_violation": bool(violations),
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: transcript_check.py AGENT RUN_DIR")
    print(json.dumps(check(sys.argv[1], sys.argv[2]), indent=2))
