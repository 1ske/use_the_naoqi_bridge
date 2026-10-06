"""Static HTML dashboard of everything the pilot has recorded (reads the records, changes nothing).

    python3 pilot/dashboard.py [--out ~/.naoqi-pilot/dashboard]   -> OUT/index.html (+ OUT/images/)

Per agent: Table 5.4's metrics over valid runs, the instruction and image check, and strip charts of distance to the
goal, completion time and cost. Per run: the manifest, the metrics, the robot's path on the floor plan (runs from
2026-10-06 14:15 on; earlier runs show only the pose at rest), the call timeline with falls and stops, tilt and heading
over time, every call with its result, the images the agent got, and its final answer. Times are seconds since t0;
the path (Mac clock) and the calls (Docker clock) line up to within about a second.
"""
import argparse
import base64
import glob
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import metrics  # noqa: E402
import success  # noqa: E402
import verify  # noqa: E402
from agent_container import AGENTS  # noqa: E402

HOME = Path.home() / ".naoqi-pilot"
WORLDS = json.loads((REPO / "pilot" / "config" / "worlds.json").read_text())


def jsonl(path):
    return metrics.read_jsonl_lenient(path) if path.exists() else []


def extract_images(harness, agent_dir, out_dir, rel):
    """The images the harness recorded, in order, saved as files: [{"src", "tool"}]."""
    images = []

    def save(data, ext, tool):
        out_dir.mkdir(parents=True, exist_ok=True)
        name = "%02d.%s" % (len(images) + 1, ext)
        (out_dir / name).write_bytes(data)
        images.append({"src": "%s/%s" % (rel, name), "tool": tool})

    if harness == "claude-code":
        names = {}
        for e in jsonl(agent_dir / "transcript.jsonl"):
            for b in ((e.get("message") or {}).get("content") or []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    names[b.get("id")] = b.get("name")
                elif b.get("type") == "tool_result":
                    for c in b.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "image":
                            src = c.get("source") or {}
                            ext = (src.get("media_type") or "image/jpeg").split("/")[-1]
                            save(base64.b64decode(src.get("data", "")), ext, names.get(b.get("tool_use_id"), "?"))
    elif harness == "opencode":
        for e in jsonl(agent_dir / "transcript.jsonl"):
            part = e.get("part") or {}
            if e.get("type") != "tool_use":
                continue
            for c in ((part.get("state") or {}).get("metadata") or {}).get("content") or []:
                uri = c.get("uri", "") if isinstance(c, dict) else ""
                m = re.match(r"data:image/(\w+);base64,(.*)", uri, re.S)
                if m:
                    save(base64.b64decode(m.group(2)), m.group(1), part.get("tool"))
    elif harness == "agy":
        for conv in sorted(agent_dir.glob("agy-brain/*/.system_generated")):
            last_call = {}
            for e in jsonl(conv / "logs" / "transcript_full.jsonl"):
                if e.get("type") == "PLANNER_RESPONSE":
                    calls = e.get("tool_calls") or []
                    last_call = calls[0] if calls else {}
                    continue
                for f in sorted((conv / "steps" / str(e.get("step_index"))).glob("media_*")):
                    save(f.read_bytes(), f.suffix.lstrip("."), last_call.get("name", "?"))
    return images


def world_geometry(task_key):
    spec = WORLDS["worlds"][task_key]
    boxes = success.boxes(spec["file"])
    text = (REPO / spec["file"]).read_text()
    start = re.search(r"DEF NAO Nao \{\s*translation\s+([-\d.]+)\s+([-\d.]+)", text)
    walls = [{"name": n, "x0": b[0], "x1": b[1], "y0": b[2], "y1": b[3], "goal": n == spec["goal"]["solid"]}
             for n, b in boxes.items() if n != "FLOOR" and b[1] - b[0] <= 8]
    return {"walls": walls, "area": success.area(task_key), "start": [float(start.group(1)), float(start.group(2))],
            "task": spec["task"]}


def run_record(run_dir, out):
    manifest = json.loads((run_dir / "manifest.json").read_text())
    harness = manifest["agent_config"]["harness"]
    try:
        m = metrics.metrics(run_dir)
    except Exception as e:  # noqa: BLE001 - shown in the dashboard, not hidden
        m = {"error": repr(e)}
    t0_file = run_dir / "agent" / "t0"
    t0 = float(t0_file.read_text()) if t0_file.exists() else None
    rel = lambda t: None if t0 is None or t is None else round(t - t0, 3)
    log = jsonl(run_dir / "calls.jsonl")
    calls = []
    for r in log:
        if r["type"] == "call":
            calls.append({"tool": r.get("tool"), "args": r.get("arguments"), "start": rel(r.get("received")),
                          "end": rel(r.get("returned")), "outcome": r.get("outcome"), "error": r.get("error"),
                          "result": r.get("result")})
    falls = [{"t": rel(r.get("time")), "cause": r.get("cause")} for r in log if r["type"] == "fall"]
    trajectory = [[rel(s["mac_t"]), s["x"], s["y"], s["heading_deg"], s["tilt_deg"]]
                  for s in jsonl(run_dir / "trajectory.jsonl")] if t0 is not None else []
    ground_truth = json.loads((run_dir / "ground_truth.json").read_text()) if (run_dir / "ground_truth.json").exists() else {}
    check_file = run_dir / "transcript_check.json"
    try:
        answer = verify.final_answer(harness, run_dir / "agent")
    except Exception:  # noqa: BLE001
        answer = ""
    return {
        "run_id": manifest["run_id"], "agent": manifest["agent"], "task_key": manifest["task_key"],
        "harness": harness, "test": bool(manifest.get("test")) or manifest["run_id"].endswith("-test"), "manifest": manifest, "metrics": m,
        "calls": calls, "falls": falls, "trajectory": trajectory, "ground_truth": ground_truth,
        "transcript_check": json.loads(check_file.read_text()) if check_file.exists() else None,
        "cost": json.loads((run_dir / "cost.json").read_text()) if (run_dir / "cost.json").exists() else None,
        "answer": answer, "said": [c["args"].get("text") for c in calls if c["tool"] == "say" and isinstance(c["args"], dict)],
        "images": extract_images(harness, run_dir / "agent", out / "images" / manifest["run_id"],
                                 "images/" + manifest["run_id"]),
        "started": manifest.get("runner_times_mac_clock", {}).get("runner_started"),
    }


def check_records():
    records = []
    for f in sorted(glob.glob(str(HOME / "checks" / "*" / "A*" / "result.json"))):
        r = json.loads(Path(f).read_text())
        if r.get("fake_llm"):
            continue
        t = r.get("tool_calls") or {}
        records.append({k: r.get(k) for k in ("agent", "check", "attempt_id", "expected", "answer", "passed",
                                              "cost_usd", "harness_version", "system_commit")}
                       | {"nao_calls": t.get("nao_tool_calls"), "exempt": {k: v for k, v in (t.get("exempt_calls") or {}).items() if v},
                          "violations": t.get("violating_calls")})
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HOME / "dashboard"))
    args = parser.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    runs = [run_record(d, out) for d in sorted(p.parent for p in (HOME / "runs").glob("*/manifest.json"))]
    data = {
        "generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "agents": {a: {"llm": s["llm"], "harness": s["harness"], "model": s["model"]}
                   for a, s in AGENTS.items() if a.startswith("A")},
        "worlds": {k: world_geometry(k) for k in WORLDS["worlds"]},
        "d": WORLDS["d_default"], "min_rtf": metrics.PROTOCOL["min_real_time_factor"]["value"],
        "runs": runs, "checks": check_records(),
    }
    html = (REPO / "pilot" / "dashboard_template.html").read_text()
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    (out / "index.html").write_text(html.replace("/*__DATA__*/null", payload))
    print(out / "index.html")


if __name__ == "__main__":
    main()
