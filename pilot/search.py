"""Runner for Search on the physical NAO (section 5.1): the operator resets the room, the runner does the rest.

    python3 pilot/search.py check-robot --nao-ip IP                 free: bridge -> NAO, NAOqi version, battery
    python3 pilot/search.py next PHASE --nao-ip IP [--count N]      the next run(s) of PHASE's Search order
    python3 pilot/search.py run AGENT --nao-ip IP                   one test run (phase test), outside any order

Run it in your own terminal: it asks the operator questions. Per run:
  1. A fresh bridge connected to the NAO at IP (NAO_IP; never the simulator), checked through /health, and the
     battery read through the bridge before the run ID is set (nothing is logged): the run starts only with the
     battery at >= min_battery_percent and not charging (protocol.json, search).
  2. The operator's checklist (robot resting at the marked start pose, ball at its marked position, charger
     unplugged, room photographed today) and Enter: the run ID is written and the agent container starts (as in
     pilot/run.py: fresh container, internal network, proxy, the agent's login).
  3. End conditions (Table 5.3): the first rest (stop signal), the caps, the harness ending, a setup component
     stopping, or the operator: type  i + Enter  for an intervention (asked for its cause; the run ends as a failure),
     x + Enter  for a fault outside the agent such as a hardware fault (asked for the reason; the run is invalid).
  4. At rest the runner says so: judge the robot where it crouched, without touching it. After the grace period and
     collection it opens the image of rest's tool result and asks for the verdict (distance from the torso, projected
     on the floor, to the ball's surface; whether part of the ball is visible in the image). The answers go to
     operator_notes.json in the run folder; pilot/metrics.py takes the success from there.
Then put the robot back at the start pose for the next run.
"""
import argparse
import datetime
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import dashboard  # noqa: E402
import order as run_order  # noqa: E402
import records  # noqa: E402
import run as runner  # noqa: E402
import transcript_check  # noqa: E402
from agent_container import AGENT_IDS, AGENTS, AgentContainer, agy_login_ok  # noqa: E402

TASK = "search"
SPEC = runner.PROTOCOL["search"]
log = runner.log


def bridge_up(nao_ip, simulator_for_test=False):
    """A fresh bridge connected to the physical NAO; returns /health."""
    if nao_ip in ("host.docker.internal", "127.0.0.1", "localhost") and not simulator_for_test:
        raise SystemExit("that is the simulator's address; give the physical NAO's IP")
    subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "bridge"], cwd=REPO, check=True,
                   capture_output=True, env=dict(os.environ, NAO_IP=nao_ip))
    runner.wait_for("the bridge's /health", lambda: runner.bridge_health().get("ok") is True, 120, period=1)
    health = runner.bridge_health()
    if health.get("naoqi") != "%s:9559" % nao_ip:
        raise SystemExit("the bridge is connected to %s, not the NAO at %s" % (health.get("naoqi"), nao_ip))
    return health


def robot_state():
    """get_state through the bridge, outside any run (no run ID: nothing is logged)."""
    request = urllib.request.Request("http://127.0.0.1:8765/call/get_state", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["result"]


def ask(question, choices=None):
    while True:
        answer = input("  %s%s " % (question, " [%s]" % "/".join(choices) if choices else "")).strip().lower()
        if not choices or answer in choices:
            return answer


def checklist():
    print("\nBefore the run (section 5.1.1):")
    answers = {
        "robot_resting_at_marked_start_pose_and_direction": ask("Robot resting (crouched) at the marked start pose, "
                                                                "facing the marked direction?", ["y", "n"]),
        "ball_at_marked_position": ask("Ball at its marked position?", ["y", "n"]),
        "charger_unplugged": ask("Charger unplugged?", ["y", "n"]),
        "room_photographed_today": ask("Room photographed today (path or 'y')?"),
        "room_unchanged_otherwise": ask("Nothing else in the room moved since the last run?", ["y", "n"]),
    }
    if any(v == "n" for v in answers.values()):
        raise SystemExit("checklist not met: %s" % ", ".join(k for k, v in answers.items() if v == "n"))
    return answers


def operator_line():
    """A line the operator typed, or None (non-blocking)."""
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.readline().strip().lower()
    return None


def rest_image(harness, agent_dir, out_dir):
    images = dashboard.extract_images(harness, agent_dir, out_dir, ".")
    rest = [i for i in images if (i.get("tool") or "").endswith("rest")]
    chosen = rest[-1] if rest else (images[-1] if images else None)
    return (out_dir / Path(chosen["src"]).name) if chosen else None, bool(rest)


def verdict(run_dir, harness):
    print("\nVerdict (section 5.1.1). Judge the robot where it rested; do not touch it before you have measured.")
    image, from_rest = rest_image(harness, run_dir / "agent", run_dir / "operator_images")
    if image:
        subprocess.run(["open", str(image)])
        print("  Opened %s%s" % (image, "" if from_rest else " (NOT identified as rest's image: check it)"))
    else:
        print("  No image of rest's tool result was found in the transcript.")
    raw = ask("Distance from the torso, projected on the floor, to the ball's surface, in m (or '>' if clearly "
              "more than %.1f m):" % SPEC["max_distance_m"])
    distance = None if raw == ">" else float(raw.replace(",", "."))
    visible = ask("Is part of the ball visible in the image of rest's tool result?", ["y", "n"]) == "y"
    facing = ask("Is the robot facing the ball (in front of it, in the bottom camera's view or under it)?", ["y", "n"])
    notes = ask("Notes (optional):")
    within = distance is not None and distance <= SPEC["max_distance_m"]
    success = within and (visible or not SPEC["require_ball_visible_in_rest_image"]) and (
        facing == "y" or not SPEC.get("require_facing_ball"))
    return {"distance_to_ball_m": distance, "within_distance": within, "ball_visible_in_rest_image": visible,
            "rest_image": str(image) if image else None, "rest_image_identified": from_rest, "facing_ball": facing,
            "success": success, "criteria": dict(SPEC), "notes": notes,
            "judged_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}


def run(agent, phase, nao_ip, slot=None, defer_cost=True, fake_script=None, checklist_given=None, chat=False):
    """fake_script: test only (phase test): the stand-in LLM plays the agent and the simulator stands in for the NAO
    (nao_ip host.docker.internal), to check this runner without the robot."""
    runner.preflight(agent, phase, fake_script)
    if phase != "test" and slot is None:
        raise SystemExit("%s runs come only from the run order (search.py next)" % phase)
    spec, prompt = AGENTS[agent], runner.PROTOCOL["prompts"][TASK]
    caps, grace = dict(runner.PROTOCOL["caps"][TASK]), runner.PROTOCOL["grace_period_s"]["value"]
    # Before any record exists: a start refused here (battery, robot, an earlier cost, changed code) is no run and
    # leaves nothing in the records.
    if spec["harness"] == "opencode" and not fake_script:
        runner.wait_for_pending_costs(agent)
    code = runner.runner_code(phase)
    (runner.INSTRUMENTATION / "run_id").unlink(missing_ok=True)
    log("starting a fresh bridge connected to the NAO at %s" % nao_ip)
    bridge_up(nao_ip, simulator_for_test=bool(fake_script))
    state = robot_state()
    battery = state.get("battery") or {}
    log("battery %s %%, charging %s, posture %s" % (battery.get("percent"), battery.get("charging"), state.get("posture")))
    if battery.get("percent") is None or battery["percent"] < SPEC["min_battery_percent"]:
        raise SystemExit("battery %s %%: a run starts only at >= %d %% (section 5.1.1); not started, nothing recorded"
                         % (battery.get("percent"), SPEC["min_battery_percent"]))
    if battery.get("charging"):
        log("WARNING: the NAO is charging (cable plugged in); recorded in the manifest")
    started = time.time()
    run_id = "%s-%s-%s" % (TASK, agent, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run_dir = records.runs_dir(phase) / run_id
    run_dir.mkdir(parents=True)
    instrumentation = runner.INSTRUMENTATION / run_id
    manifest = {
        "run_id": run_id, "phase": phase, "task": "Search", "task_key": TASK, "agent": agent, "robot": "physical NAO",
        "nao_ip": nao_ip,
        "run_order": {"file": str(records.order_file(phase, TASK)), "seed": slot["order_seed"],
                      "position": slot["position"], "block": slot["block"], "attempt": slot["attempt"]}
        if slot else None,
        "agent_config": spec, "deviations": AGENTS["deviations"][spec["deviations_ref"]],
        "harness_version": runner.LOCK["harness_versions"][runner.HARNESS_KEY[spec["harness"]]],
        "system_commit": runner.git("rev-parse", "HEAD"),
        "system_uncommitted_changes": bool(runner.git("status", "--porcelain")), "runner_code": code,
        "prompt": prompt, "caps": caps, "grace_period_s": grace, "search_criteria": SPEC,
        "protocol_values_decided": {k: v["decided"] for k, v in runner.PROTOCOL.items()
                                    if isinstance(v, dict) and "decided" in v},
        "test": {"fake_llm": True, "simulator_as_robot": True,
                 "note": "Stand-in LLM and simulator: a check of search.py, not a real run."} if fake_script else None,
    }
    if spec["harness"] in runner.PRICED_MODEL:
        manifest["price"] = dict(runner.PRICES[runner.PRICED_MODEL[spec["harness"]]],
                                 model=runner.PRICED_MODEL[spec["harness"]])
    log("run %s (%s, %s)" % (run_id, agent, spec["llm"]))
    container = AgentContainer(run_id, agent, run_dir / "agent",
                               test_env=runner.fake_llm_env(spec["harness"]) if fake_script else None,
                               proxy_allow=[r"^no-host-allowed-in-fake-llm-tests\.invalid$"] if fake_script else None)
    end, invalid, times, interventions = None, None, {}, []
    try:
        if spec["harness"] == "agy" and not fake_script and not agy_login_ok(run_id):
            end, invalid = "setup_failure", "the agy login has expired (agy models failed)"
            raise RuntimeError(invalid)
        manifest["robot_before"] = {"battery": battery, "posture": state.get("posture"), "fallen": state.get("fallen"),
                                    "awake": state.get("awake")}
        manifest["operator_checklist"] = checklist_given or checklist()
        if spec["harness"] == "opencode" and not fake_script:
            container.openrouter_key = runner.openrouter_key(agent)
            times["usage_before_read"] = time.time()
            usage_before = runner.openrouter_usage(container.openrouter_key)
            runner.write_cost_pending(run_dir, usage_before)
        if not chat:
            input("\nPress Enter to start the run (the prompt is submitted right away) ")
        OPERATOR_END.unlink(missing_ok=True)
        runner.write_atomic(runner.INSTRUMENTATION / "run_id", run_id)
        container.bridge = runner.compose(REPO, "ps", "-q", "bridge")
        container.setup_network()
        if fake_script:
            runner.start_fake_llm(container, fake_script)
        manifest["image"] = {"name": spec["image"], "id": container.create(prompt)}
        if fake_script and spec["harness"] == "agy":
            runner.stage_agy_fake_settings(container)
        container.start()
        times["container_started"] = time.time()
        print("\nRunning. Type  i + Enter  for an intervention,  x + Enter  for a fault outside the agent.\n")
        t0_file, calls_file = run_dir / "agent" / "t0", instrumentation / "calls.jsonl"
        t0_seen = None
        while end is None:
            now = time.time()
            if t0_seen is None and t0_file.exists() and t0_file.read_text().strip():
                t0_seen = times["t0_seen"] = now
                log("t0: prompt submitted")
            calls = [r for r in runner.read_jsonl(calls_file) if r.get("type") == "call"]
            line = None if chat else operator_line()
            if OPERATOR_END.exists():
                order = json.loads(OPERATOR_END.read_text())
                OPERATOR_END.unlink()
                if order["kind"] == "intervention":
                    times["intervention"] = order["time"]
                    interventions.append({"time": order["time"], "cause": order["cause"], "via": "chat",
                                          "t_since_t0_s": round(order["time"] - t0_seen, 1) if t0_seen else None})
                    end = "intervention"
                else:
                    end, invalid = "setup_failure", "operator: %s" % order["cause"]
            elif line == "i":
                times["intervention"] = now
                cause = input("  Cause of the intervention: ").strip()
                interventions.append({"time": now, "t_since_t0_s": round(now - t0_seen, 1) if t0_seen else None,
                                      "cause": cause})
                end = "intervention"
            elif line == "x":
                end, invalid = "setup_failure", "operator: %s" % input("  Fault (e.g. hardware fault): ").strip()
            elif any(c["tool"] == "rest" for c in calls) or (instrumentation / "rest_received").exists():
                end = "stop_signal"
            elif len(calls) >= caps["tool_calls"]:
                end = "tool_call_cap"
            elif t0_seen is not None and now - t0_seen >= caps["wall_clock_s"]:
                end = "wall_clock_cap"
            elif not container.running():
                time.sleep(1.0)
                calls = [r for r in runner.read_jsonl(calls_file) if r.get("type") == "call"]
                end = "stop_signal" if any(c["tool"] == "rest" for c in calls) else "session_ended"
                if t0_seen is None:
                    invalid = "the harness exited before submitting the prompt"
            elif not runner.service_running(REPO, "bridge"):
                end, invalid = "setup_failure", "the bridge stopped"
            elif t0_seen is None and now - times["container_started"] > runner.T0_TIMEOUT:
                end, invalid = "setup_failure", "no t0 within %d s of the container start" % runner.T0_TIMEOUT
            else:
                time.sleep(runner.POLL)
        times["end_detected"] = time.time()
        runner.clear_run_id(run_id)
        if end == "stop_signal":
            print("\n\a*** REST: the robot is crouching where the agent stopped it. Do not touch it; judge it now. ***\n")
        log("end: %s%s" % (end, " (invalid: %s)" % invalid if invalid else ""))
        exited_in_grace = None
        if end == "stop_signal" and container.running():
            deadline = time.time() + grace
            while container.running() and time.time() < deadline:
                time.sleep(runner.POLL)
            exited_in_grace = not container.running()
        if container.running():
            container.stop()  # an intervention or cap: the MCP server goes away, so the bridge rests the robot
        times["harness_stopped"] = time.time()
        container.collect()
        if (instrumentation / "calls.jsonl").exists():
            shutil.copy(instrumentation / "calls.jsonl", run_dir / "calls.jsonl")
        check = transcript_check.check(agent, run_dir / "agent")
        (run_dir / "transcript_check.json").write_text(json.dumps(check, indent=2))
        if not check["transcript_found"] and not invalid:
            invalid = "no transcript: the transcript check could not be made"
        notes = {"interventions": interventions, "invalid_reason": None, "verdict": None}
        if end == "stop_signal" and not invalid:
            if chat:
                image, from_rest = rest_image(spec["harness"], run_dir / "agent", run_dir / "operator_images")
                notes["verdict_pending"] = True
                notes["rest_image"] = str(image) if image else None
                notes["rest_image_identified"] = from_rest
                log("verdict pending: python3 pilot/search.py verdict %s --distance M --visible y|n" % run_dir.name)
            else:
                notes["verdict"] = verdict(run_dir, spec["harness"])
                print("  -> %s" % ("SUCCESS" if notes["verdict"]["success"] else "failure"))
        naoqi_falls = sum(1 for r in runner.read_jsonl(run_dir / "calls.jsonl") if r.get("type") == "fall")
        if naoqi_falls:
            log("NAOqi reported %d fall(s): ask the operator, then python3 pilot/search.py falls %s N --notes TEXT"
                % (naoqi_falls, run_dir.name))
        (run_dir / "operator_notes.json").write_text(json.dumps(notes, indent=2))
        if container.openrouter_key:
            # In the background, so that the verdict and the next run need not wait for OpenRouter; a marker file
            # makes the same agent's next run wait, also from another process (search.py start).
            runner.settle_in_background(agent, run_id, run_dir, container.openrouter_key, usage_before, spec["model"])
            log("OpenRouter cost of %s: settling in the background (only %s's next run waits for it)" % (run_id, agent))
        manifest.update({"end_condition": end, "invalid_reason": invalid,
                         "harness_exited_in_grace_period": exited_in_grace,
                         "harness_exit_code": container.exit_code()})
    except BaseException as e:
        manifest.update({"end_condition": end or "runner_error",
                         "invalid_reason": invalid or "runner error: %r" % e})
        raise
    finally:
        runner.clear_run_id(run_id)
        container.remove()
        manifest["runner_times_mac_clock"] = {k: runner.utc(v) for k, v in dict(times, runner_started=started).items()}
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        log("run folder: %s" % run_dir)
    print("\nPut the robot back at the start pose (and check the ball) before the next run.")
    return run_dir


OPERATOR_END = runner.INSTRUMENTATION / "operator_end.json"  # written by `search.py intervene|fault` (chat mode)


def operator_end(kind, cause):
    runner.write_atomic(OPERATOR_END, json.dumps({"kind": kind, "cause": cause, "time": time.time()}))
    print("recorded: %s (%s); the running run ends now" % (kind, cause))


def record_verdict(run_name, distance, visible, facing, notes_text, phase):
    run_dir = next((d for p in records.PHASES for d in [records.runs_dir(p) / run_name] if d.exists()), None)
    if run_dir is None:
        raise SystemExit("no run folder %s" % run_name)
    notes_file = run_dir / "operator_notes.json"
    notes = json.loads(notes_file.read_text())
    if notes.get("verdict"):
        raise SystemExit("%s already has a verdict" % run_name)
    d = None if distance == ">" else float(distance.replace(",", "."))
    within = d is not None and d <= SPEC["max_distance_m"]
    vis = visible == "y"
    notes["verdict"] = {"distance_to_ball_m": d, "within_distance": within, "ball_visible_in_rest_image": vis,
                        "rest_image": notes.get("rest_image"), "rest_image_identified": notes.get("rest_image_identified"),
                        "facing_ball": facing, "success": within and (vis or not SPEC["require_ball_visible_in_rest_image"])
                        and (facing == "y" or not SPEC.get("require_facing_ball")),
                        "criteria": dict(SPEC), "notes": notes_text, "given_via": "chat with the runner's operator",
                        "judged_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    notes["verdict_pending"] = False
    notes_file.write_text(json.dumps(notes, indent=2))
    print("%s: %s" % (run_name, "SUCCESS" if notes["verdict"]["success"] else "failure"))


def record_falls(run_name, count, notes_text):
    """The falls the operator saw (Search: the fall count, decided by the author 2026-10-19 after NAOqi reported a fall
    in search-A7-20261019T103145Z that the operator did not see; pilot/metrics.py uses this count when it is given)."""
    run_dir = next((d for p in records.PHASES for d in [records.runs_dir(p) / run_name] if d.exists()), None)
    if run_dir is None:
        raise SystemExit("no run folder %s" % run_name)
    notes_file = run_dir / "operator_notes.json"
    notes = json.loads(notes_file.read_text())
    if notes.get("falls_observed"):
        raise SystemExit("%s already has the operator's fall count" % run_name)
    notes["falls_observed"] = {
        "count": count,
        "naoqi_fall_events": sum(1 for r in runner.read_jsonl(run_dir / "calls.jsonl") if r.get("type") == "fall"),
        "notes": notes_text, "given_via": "chat with the runner's operator",
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    notes_file.write_text(json.dumps(notes, indent=2))
    print("%s: %d fall(s) seen by the operator, %d reported by NAOqi" % (run_name, count,
                                                                       notes["falls_observed"]["naoqi_fall_events"]))


def pending_verdicts(phase):
    out = []
    for d in records.runs_of(phase):
        f = d / "operator_notes.json"
        if d.name.startswith("search-") and f.exists() and json.loads(f.read_text()).get("verdict_pending"):
            out.append(d.name)
    return out


def run_next(phase, nao_ip, count):
    import metrics
    done = []
    for i in range(count):
        slot = run_order.next_slot(phase, TASK)
        if slot is None:
            log("every position of the %s order for Search has a valid run" % phase)
            break
        log("position %d (block %d), %s, attempt %d" % (slot["position"], slot["block"], slot["agent"], slot["attempt"]))
        run_dir = run(slot["agent"], phase, nao_ip, slot)
        done.append(run_dir)
        m = metrics.metrics(run_dir)
        if not m["valid"]:
            log("invalid run (%s); position %d stays open" % ("; ".join(m["invalid_reasons"]), slot["position"]))
        if i + 1 < count and ask("Next run?", ["y", "n"]) == "n":
            break
    runner.wait_for_pending_costs()
    return done


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-robot")
    p.add_argument("--nao-ip", required=True)
    p = sub.add_parser("next")
    p.add_argument("phase", choices=records.PHASES)
    p.add_argument("--nao-ip", required=True)
    p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("start", help="chat mode: the next run of the order, no prompts (checklist confirmed in chat)")
    p.add_argument("phase", choices=records.PHASES)
    p.add_argument("--nao-ip", required=True)
    p.add_argument("--checklist", required=True, help="what the operator confirmed, e.g. 'pose,ball,photo=IMG_1234'")
    p.add_argument("--agent", choices=AGENT_IDS, help="phase test only: this agent instead of the order")
    p.add_argument("--fake-llm", metavar="SCRIPT", help="phase test only: stand-in LLM, simulator as the robot")
    p = sub.add_parser("intervene"); p.add_argument("cause")
    p = sub.add_parser("fault"); p.add_argument("reason")
    p = sub.add_parser("verdict")
    p.add_argument("run")
    p.add_argument("--distance", required=True, help="m, torso (on the floor) to the ball's surface, or '>'")
    p.add_argument("--visible", choices=["y", "n"], required=True)
    p.add_argument("--facing", choices=["y", "n"], default="")
    p.add_argument("--notes", default="")
    p = sub.add_parser("falls", help="the falls the operator saw in a run (when NAOqi reported a fall)")
    p.add_argument("run")
    p.add_argument("count", type=int)
    p.add_argument("--notes", default="")
    p = sub.add_parser("run")
    p.add_argument("agent", choices=AGENT_IDS)
    p.add_argument("--nao-ip", required=True)
    p.add_argument("--fake-llm", metavar="SCRIPT", help="check this runner: stand-in LLM, and the simulator "
                   "(--nao-ip host.docker.internal) as the robot")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if args.cmd == "check-robot":
        (runner.INSTRUMENTATION / "run_id").unlink(missing_ok=True)
        print(json.dumps(bridge_up(args.nao_ip), indent=2))
        s = robot_state()
        print(json.dumps({k: s.get(k) for k in ("battery", "posture", "fallen", "awake", "sonar", "warnings")}, indent=2))
    elif args.cmd == "start":
        pending = [r for p in records.PHASES for r in pending_verdicts(p)]
        if pending:
            raise SystemExit("record the verdict first: %s" % ", ".join(pending))
        given = {"confirmed_by_operator_in_chat": args.checklist}
        if args.phase == "test":
            print(run(args.agent or "A1", "test", args.nao_ip, defer_cost=False, checklist_given=given, chat=True,
                      fake_script=args.fake_llm))
        else:
            # Skip positions whose run is still being finished (its folder has no manifest yet, e.g. a cost settling).
            busy = {d.name.split("-")[1] for d in records.runs_dir(args.phase).glob(TASK + "-*")
                    if not (d / "manifest.json").exists()}
            order_, rows = run_order.status(args.phase, TASK)
            slot = next((dict(r, attempt=len(r["attempts"]) + 1, order_seed=order_["seed"]) for r in rows
                         if not r["done"] and r["agent"] not in busy), None)
            if busy:
                log("still being finished: %s (skipped)" % ", ".join(sorted(busy)))
            if slot is None:
                raise SystemExit("every position of the %s order for Search has a valid run" % args.phase)
            log("position %d (block %d), %s, attempt %d" % (slot["position"], slot["block"], slot["agent"],
                                                           slot["attempt"]))
            print(run(slot["agent"], args.phase, args.nao_ip, slot, defer_cost=True, checklist_given=given, chat=True))
    elif args.cmd == "intervene":
        operator_end("intervention", args.cause)
    elif args.cmd == "fault":
        operator_end("fault", args.reason)
    elif args.cmd == "verdict":
        record_verdict(args.run, args.distance, args.visible, args.facing, args.notes, None)
    elif args.cmd == "falls":
        record_falls(args.run, args.count, args.notes)
    elif args.cmd == "next":
        for path in run_next(args.phase, args.nao_ip, args.count):
            print(path)
    else:
        print(run(args.agent, "test", args.nao_ip, defer_cost=False, fake_script=args.fake_llm))


if __name__ == "__main__":
    main()
