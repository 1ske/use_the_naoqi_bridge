"""Runner for one simulated run (section 5.1.3): reset, start, watch for the end, grace period, collect, clean up.

    python3 pilot/run.py AGENT TASK [--position N] [--fake-llm SCRIPT.json]

Needs Webots open on the task's world (pilot/config/worlds.json) in real-time mode, with nothing attached to the
`truth` robot, and Docker. Per run:
  1. Reset: stop the bridge, reload the world (NAOQI_ATTEMPT_v1's reload controller, as the truth robot), start a
     fresh naoqisim and wait for "NAOqi is ready", start the ground-truth supervisor, write the run ID, check that
     the supervisor sees the right world in real-time mode, start a fresh bridge and wait for /health.
  2. Start the agent container (pilot/agent_container.py). Its pilot-agent writes /out/t0 when it submits the
     prompt (t0, Docker VM clock, the bridge's clock); the runner passes that signal on to the supervisor.
  3. Watch the bridge's call log for the first rest (normal end: the bridge has returned its result), the
     tool-call cap or the wall-clock cap (Table 5.3), the harness exiting without rest (session ended), or a
     setup component stopping (invalid).
  4. At the end: signal the supervisor and remove the run ID (the bridge stops logging), give the harness the
     grace period after a normal end, stop it, and collect transcript, call log and ground truth into
     ~/.naoqi-pilot/runs/<run_id>/ with manifest.json and transcript_check.json. Remove the container.

--fake-llm (free tests only, Claude Code agents): the harness talks to pilot/tools/fake_llm.py, which plays the
script, on the run's internal network. The login is a fake token, the template login is not read, and the proxy
allows no host, so no request leaves the machine.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import transcript_check  # noqa: E402
from agent_container import AGENTS, AgentContainer, docker  # noqa: E402

PILOT_HOME = Path.home() / ".naoqi-pilot"
INSTRUMENTATION = PILOT_HOME / "instrumentation"
RUNS = PILOT_HOME / "runs"
NAOQISIM_DIR = Path(os.environ.get("PILOT_NAOQISIM_DIR", REPO.parent / "NAOQI_ATTEMPT_v1"))
WEBOTS_HOME = Path(os.environ.get("WEBOTS_HOME", "/Applications/Webots.app"))
CONTROLLER = WEBOTS_HOME / "Contents" / "MacOS" / "webots-controller"
RELOAD = NAOQISIM_DIR / "webots" / "tools" / "reload.py"
SUPERVISOR = REPO / "pilot" / "supervisor" / "pilot_supervisor.py"
PROTOCOL = json.loads((REPO / "pilot" / "config" / "protocol.json").read_text())
WORLDS = json.loads((REPO / "pilot" / "config" / "worlds.json").read_text())
LOCK = json.loads((REPO / "pilot" / "images.lock.json").read_text())
HARNESS_KEY = {"claude-code": "claude", "opencode": "opencode", "agy": "agy"}  # images.lock.json's names
BRIDGE_HEALTH = "http://127.0.0.1:8765/health"
SIM_NAOQI = "host.docker.internal:9559"  # naoqisim's published port; a simulated run never uses another NAOqi
POLL = 0.1
T0_TIMEOUT = 120  # s from container start; OpenCode alone waits up to 60 s for the MCP server


def log(message):
    print("%s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), message), file=sys.stderr, flush=True)


def utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="milliseconds")


def compose(directory, *args, check=True):
    result = subprocess.run(["docker", "compose", *args], cwd=directory, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError("docker compose %s failed: %s" % (" ".join(args), result.stderr.strip()[-300:]))
    return result.stdout.strip()


def service_running(directory, service):
    return compose(directory, "ps", "-a", service, "--format", "{{.State}}", check=False) == "running"


def wait_for(what, condition, timeout, period=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(period)
    raise RuntimeError("timed out after %d s waiting for %s" % (timeout, what))


def write_atomic(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def read_jsonl(path):
    records = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                pass  # a line being appended right now
    return records


def bridge_health():
    try:
        with urllib.request.urlopen(BRIDGE_HEALTH, timeout=3) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return {}


# --- reset (section 5.1.1: world reloaded, naoqisim and bridge restarted, robot resting at the start pose) ------
def reset(run_id, world):
    if subprocess.run(["pgrep", "-f", "robot-name=truth"], capture_output=True).returncode == 0:
        raise RuntimeError("a controller is attached to the truth robot (truth.py, a supervisor?): stop it first")
    (INSTRUMENTATION / "run_id").unlink(missing_ok=True)
    log("stopping the bridge (it rests the robot)")
    compose(REPO, "stop", "bridge")
    log("reloading the world")
    env = dict(os.environ, WEBOTS_HOME=str(WEBOTS_HOME))
    for attempt in (1, 2, 3):
        # Webots applies the reload only while the NAO's extern controller (naoqisim) is connected; naoqisim
        # exiting is the proof that it happened (NAOQI_ATTEMPT_v1/tools/trials.sh).
        compose(NAOQISIM_DIR, "up", "-d", "naoqisim")
        try:
            subprocess.run([str(CONTROLLER), "--robot-name=truth", str(RELOAD)], env=env, timeout=30,
                           capture_output=True)
        except subprocess.TimeoutExpired:
            pass
        try:
            wait_for("naoqisim to exit after the reload", lambda: not service_running(NAOQISIM_DIR, "naoqisim"), 30)
            break
        except RuntimeError:
            log("reload attempt %d: naoqisim still running" % attempt)
    else:
        raise RuntimeError("world reload failed (is Webots open and running, with a robot named truth?)")
    time.sleep(2)
    log("starting a fresh naoqisim")
    compose(NAOQISIM_DIR, "up", "-d", "--force-recreate", "naoqisim")
    wait_for("NAOqi is ready", lambda: "NAOqi is ready" in compose(NAOQISIM_DIR, "logs", "naoqisim", check=False),
             300, period=2)
    log("starting the ground-truth supervisor")
    supervisor_log = open(PILOT_HOME / "supervisor.log", "a")
    supervisor = subprocess.Popen([str(CONTROLLER), "--robot-name=truth", str(SUPERVISOR)], env=env,
                                  stdout=supervisor_log, stderr=subprocess.STDOUT)
    try:
        write_atomic(INSTRUMENTATION / "run_id", run_id)
        ground_truth = INSTRUMENTATION / run_id / "ground_truth.json"
        wait_for("the supervisor to attach", ground_truth.exists, 30)
        attached = json.loads(ground_truth.read_text())
        if attached["world"] != Path(world["file"]).name or not attached["real_time_mode"]:
            raise RuntimeError("Webots has %s open (real-time mode: %s); the task needs %s in real-time mode"
                               % (attached["world"], attached["real_time_mode"], Path(world["file"]).name))
        log("starting a fresh bridge")
        # NAO_IP set here, so that neither the shell nor .env can point a simulated run at a physical NAO.
        subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "bridge"], cwd=REPO, check=True,
                       capture_output=True, env=dict(os.environ, NAO_IP=SIM_NAOQI.split(":")[0]))
        wait_for("the bridge's /health", lambda: bridge_health().get("ok") is True, 120, period=1)
        if bridge_health().get("naoqi") != SIM_NAOQI:
            raise RuntimeError("the bridge is connected to %s, not naoqisim" % bridge_health().get("naoqi"))
    except BaseException:
        stop_supervisor(supervisor)
        raise
    return supervisor


def stop_supervisor(supervisor):
    if supervisor.poll() is None:
        supervisor.terminate()
        try:
            supervisor.wait(10)
        except subprocess.TimeoutExpired:
            supervisor.kill()


# --- fake LLM (tests only) ------------------------------------------------------------------------------------
def start_fake_llm(container, script):
    cap = PILOT_HOME / "fake-llm" / container.run_id
    cap.mkdir(parents=True, exist_ok=True)
    shutil.copy(script, cap / "script.json")
    name = "pilot-%s-fakellm" % container.run_id
    docker("run", "-d", "--name", name, "--network", container.net, "--network-alias", "fake-llm",
           "--entrypoint", "python", "-v", "%s:/fake_llm.py:ro" % (REPO / "pilot" / "tools" / "fake_llm.py"),
           "-v", "%s:/cap" % cap, "naoqi-pilot/base", "/fake_llm.py")
    container.created.append(("container", name))
    return cap


def fake_llm_env(harness):
    if harness != "claude-code":
        raise SystemExit("--fake-llm is implemented for Claude Code agents only")
    return {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake-llm-test-not-a-token",
            "ANTHROPIC_BASE_URL": "http://fake-llm:8080", "NO_PROXY": "fake-llm"}


# --- one run -------------------------------------------------------------------------------------------------
def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()


def run(agent, task, position=None, fake_script=None, test_caps=None):
    spec, world = AGENTS[agent], WORLDS["worlds"][task]
    prompt = PROTOCOL["prompts"][task]
    caps, grace = dict(PROTOCOL["caps"][task], **(test_caps or {})), PROTOCOL["grace_period_s"]["value"]
    started = time.time()
    run_id = "%s-%s-%s%s" % (task, agent, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                             "-test" if fake_script else "")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)
    instrumentation = INSTRUMENTATION / run_id
    world_file = REPO / world["file"]
    manifest = {
        "run_id": run_id, "task": world["task"], "task_key": task, "agent": agent, "position_in_run_order": position,
        "agent_config": spec, "deviations": AGENTS["deviations"][spec["deviations_ref"]],
        "harness_version": LOCK["harness_versions"][HARNESS_KEY[spec["harness"]]],
        "system_commit": git("rev-parse", "HEAD"), "system_uncommitted_changes": bool(git("status", "--porcelain")),
        "world_file": world["file"], "world_sha256": hashlib.sha256(world_file.read_bytes()).hexdigest(),
        "prompt": prompt, "caps": caps, "grace_period_s": grace,
        "protocol_values_decided": {k: PROTOCOL[k]["decided"] for k in ("prompts", "caps", "grace_period_s")},
        "test": None,
    }
    log("run %s" % run_id)
    supervisor = None
    test_env = fake_llm_env(spec["harness"]) if fake_script else None
    container = AgentContainer(run_id, agent, run_dir / "agent", bridge_container=None, test_env=test_env,
                               proxy_allow=[r"^no-host-allowed-in-fake-llm-tests\.invalid$"] if fake_script else None)
    end, invalid, times = None, None, {}
    try:
        supervisor = reset(run_id, world)
        container.bridge = compose(REPO, "ps", "-q", "bridge")
        container.setup_network()
        if fake_script:
            cap = start_fake_llm(container, fake_script)
            manifest["test"] = {"fake_llm": True, "caps_override": test_caps,
                                "script": json.loads(Path(fake_script).read_text()),
                                "fake_llm_requests": str(cap / "requests.jsonl"),
                                "note": "Fake LLM (pilot/tools/fake_llm.py), fake login, no egress: not a real run."}
        manifest["image"] = {"name": spec["image"], "id": container.create(prompt)}
        log("starting the agent")
        container.start()
        times["container_started"] = time.time()
        t0_file, calls_file = run_dir / "agent" / "t0", instrumentation / "calls.jsonl"
        t0_seen = None
        while end is None:
            now = time.time()
            if t0_seen is None and t0_file.exists() and t0_file.read_text().strip():
                write_atomic(instrumentation / "t0", t0_file.read_text())  # the supervisor's start mark
                t0_seen = times["t0_seen"] = now
                log("t0: prompt submitted")
            calls = [r for r in read_jsonl(calls_file) if r.get("type") == "call"]
            if any(c["tool"] == "rest" for c in calls):
                end = "stop_signal"
            elif len(calls) >= caps["tool_calls"]:
                end = "tool_call_cap"
            elif t0_seen is not None and now - t0_seen >= caps["wall_clock_s"]:
                end = "wall_clock_cap"
            elif not container.running():
                time.sleep(1.0)  # the bridge writes a call's record just after replying
                calls = [r for r in read_jsonl(calls_file) if r.get("type") == "call"]
                end = "stop_signal" if any(c["tool"] == "rest" for c in calls) else "session_ended"
                if t0_seen is None:
                    invalid = "the harness exited before submitting the prompt"
            elif not service_running(NAOQISIM_DIR, "naoqisim"):
                end, invalid = "setup_failure", "naoqisim stopped"
            elif not service_running(REPO, "bridge"):
                end, invalid = "setup_failure", "the bridge stopped"
            elif supervisor.poll() is not None:
                end, invalid = "setup_failure", "the ground-truth supervisor stopped"
            elif t0_seen is None and now - times["container_started"] > T0_TIMEOUT:
                end, invalid = "setup_failure", "no t0 within %d s of the container start" % T0_TIMEOUT
            else:
                time.sleep(POLL)
        times["end_detected"] = time.time()
        # End of the run: the supervisor takes the real-time factor, and the bridge stops logging.
        write_atomic(instrumentation / "end", "%.6f\n" % times["end_detected"])
        (INSTRUMENTATION / "run_id").unlink(missing_ok=True)
        log("end: %s%s" % (end, " (invalid: %s)" % invalid if invalid else ""))
        exited_in_grace = None
        if end == "stop_signal" and container.running():
            log("grace period: up to %d s for the harness to finish" % grace)
            deadline = time.time() + grace
            while container.running() and time.time() < deadline:
                time.sleep(POLL)
            exited_in_grace = not container.running()
        if container.running():
            container.stop()
        times["harness_stopped"] = time.time()
        container.collect()
        if t0_seen is not None:
            gt = instrumentation / "ground_truth.json"
            try:
                wait_for("the real-time factor", lambda: json.loads(gt.read_text())["real_time_factor"] is not None, 5)
            except RuntimeError:
                log("WARNING: the supervisor saved no real-time factor")
        for name in ("calls.jsonl", "ground_truth.json"):
            if (instrumentation / name).exists():
                shutil.copy(instrumentation / name, run_dir / name)
        check = transcript_check.check(agent, run_dir / "agent")
        (run_dir / "transcript_check.json").write_text(json.dumps(check, indent=2))
        if not check["transcript_found"] and not invalid:
            invalid = "no transcript: the transcript check could not be made"
        manifest.update({"end_condition": end, "invalid_reason": invalid,
                         "harness_exited_in_grace_period": exited_in_grace,
                         "harness_exit_code": container.exit_code()})
    except BaseException as e:
        manifest.update({"end_condition": end or "runner_error", "invalid_reason": "runner error: %r" % e})
        raise
    finally:
        (INSTRUMENTATION / "run_id").unlink(missing_ok=True)
        container.remove()
        if supervisor is not None:
            stop_supervisor(supervisor)
        manifest["runner_times_mac_clock"] = {k: utc(v) for k, v in dict(times, runner_started=started).items()}
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        log("run folder: %s" % run_dir)
    return run_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent", choices=[a for a in AGENTS if a.startswith("A")])
    parser.add_argument("task", choices=list(WORLDS["worlds"]))
    parser.add_argument("--position", type=int, help="position in the run order")
    parser.add_argument("--fake-llm", metavar="SCRIPT", help="test run against pilot/tools/fake_llm.py")
    parser.add_argument("--test-caps", metavar="NAME=VALUE,...", help="test runs only: override the task's caps, "
                        "e.g. tool_calls=3 or wall_clock_s=20")
    args = parser.parse_args()
    test_caps = None
    if args.test_caps:
        if not args.fake_llm:
            parser.error("--test-caps is for --fake-llm test runs only")
        test_caps = {k: int(v) for k, v in (item.split("=") for item in args.test_caps.split(","))}
        unknown = set(test_caps) - set(PROTOCOL["caps"][args.task])
        if unknown:
            parser.error("unknown caps: %s" % ", ".join(sorted(unknown)))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # clean up (container, run ID) when killed
    print(run(args.agent, args.task, args.position, args.fake_llm, test_caps))
