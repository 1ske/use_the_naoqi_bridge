"""Runner for one simulated run (section 5.1.3): reset, start, watch for the end, grace period, collect, clean up.

    python3 pilot/run.py next PHASE TASK [--count N] [--fake-llm SCRIPT.json]   the next run of the run order
    python3 pilot/run.py run AGENT TASK [--fake-llm SCRIPT.json] [--test-caps ...] one test run, outside any order

PHASE is test, pilot or main (pilot/records.py); runs go to ~/.naoqi-pilot/grid/PHASE/runs/<run_id>/. Pilot and main
runs come only from the run order (pilot/order.py): `next` takes the first position without a valid run, so an invalid
run is repeated in the same position, and every attempt is kept with its position and attempt number. `run` makes a
test run (phase test). --fake-llm runs are always phase test. Non-test runs need the agent's model ID confirmed in
pilot/config/agents.json, and main runs need every value in pilot/config/protocol.json decided.

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
     ~/.naoqi-pilot/grid/PHASE/runs/<run_id>/ with manifest.json and transcript_check.json. Remove the container.

--fake-llm (free tests only): the harness talks to pilot/tools/fake_llm.py, which plays the
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
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import order as run_order  # noqa: E402
import records  # noqa: E402
import transcript_check  # noqa: E402
from agent_container import AGENT_IDS, AGENTS, AgentContainer, agy_login_ok, docker  # noqa: E402

PILOT_HOME = records.PILOT_HOME
INSTRUMENTATION = PILOT_HOME / "instrumentation"
NAOQISIM_DIR = Path(os.environ.get("PILOT_NAOQISIM_DIR", REPO.parent / "NAOQI_ATTEMPT_v1"))
WEBOTS_HOME = Path(os.environ.get("WEBOTS_HOME", "/Applications/Webots.app"))
CONTROLLER = WEBOTS_HOME / "Contents" / "MacOS" / "webots-controller"
RELOAD = NAOQISIM_DIR / "webots" / "tools" / "reload.py"
SUPERVISOR = REPO / "pilot" / "supervisor" / "pilot_supervisor.py"
PROTOCOL = json.loads((REPO / "pilot" / "config" / "protocol.json").read_text())
WORLDS = json.loads((REPO / "pilot" / "config" / "worlds.json").read_text())
LOCK = json.loads((REPO / "pilot" / "images.lock.json").read_text())
PRICES = json.loads((REPO / "pilot" / "config" / "prices.json").read_text())
PRICED_MODEL = {"agy": "gemini-3.8-flash"}  # harnesses whose cost is their token counts at a list price
HARNESS_KEY = {"claude-code": "claude", "opencode": "opencode", "agy": "agy"}  # images.lock.json's names
BRIDGE_HEALTH = "http://127.0.0.1:8765/health"
SIM_NAOQI = "host.docker.internal:9559"  # naoqisim's published port; a simulated run never uses another NAOqi
POLL = 0.1
def openrouter_key(agent):
    """The OpenCode agent's own OpenRouter key (agents.json openrouter_key_file; one key per agent since 2026-10-15)."""
    return Path(AGENTS[agent]["openrouter_key_file"]).expanduser().read_text().strip()
OPENROUTER_KEY_INFO = "https://openrouter.ai/api/v1/key"
USAGE_FIRST_READ, USAGE_POLL, USAGE_STABLE_READS, USAGE_MAX_WAIT = 60, 10, 3, 1200  # s, s, reads in a row, s
T0_TIMEOUT = 120  # s from container start; OpenCode alone waits up to 60 s for the MCP server


def clear_run_id(run_id):
    """Remove the bridge's run ID only if it is this run's: another runner's run may be going (2026-10-15: an earlier
    Search run's process, finishing its OpenRouter cost, removed the next run's ID and cut its call log)."""
    path = INSTRUMENTATION / "run_id"
    try:
        if path.read_text().strip() == run_id:
            path.unlink()
    except FileNotFoundError:
        pass


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
    if harness == "claude-code":
        return {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake-llm-test-not-a-token",
                "ANTHROPIC_BASE_URL": "http://fake-llm:8080", "NO_PROXY": "fake-llm"}
    if harness == "opencode":
        return {"OPENROUTER_API_KEY": "sk-or-v1-fake-llm-test-not-a-key", "NO_PROXY": "fake-llm",
                "OPENCODE_CONFIG_CONTENT": json.dumps(
                    {"providers": {"openrouter": {"settings": {"baseURL": "http://fake-llm:8080/api/v1"}}}})}
    if harness == "agy":  # plus the settings staged by stage_agy_fake_settings
        return {"GEMINI_API_KEY": "fake-llm-test-not-a-key", "GOOGLE_GEMINI_BASE_URL": "http://fake-llm:8080",
                "NO_PROXY": "fake-llm"}
    raise SystemExit("--fake-llm is not implemented for %s" % harness)


def stage_agy_fake_settings(container):
    """agy reaches another endpoint only with modelProvider=gemini: the image's settings plus that, staged into the
    container like a login (pilot/tools/capture_check.sh does the same)."""
    settings = json.loads(docker("run", "--rm", "--entrypoint", "cat", container.spec["image"],
                                 "/home/agent/.gemini/antigravity-cli/settings.json"))
    settings["modelProvider"] = "gemini"
    with tempfile.TemporaryDirectory() as stage:
        target = Path(stage) / ".gemini" / "antigravity-cli"
        target.mkdir(parents=True)
        (target / "settings.json").write_text(json.dumps(settings))
        docker("cp", stage + "/.", "%s:/stage" % container.name)


# --- cost of OpenCode runs: the shared OpenRouter key's usage before and after the run ---------------------------
def openrouter_usage(key):
    request = urllib.request.Request(OPENROUTER_KEY_INFO, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=15) as response:
        return float(json.loads(response.read())["data"]["usage"])


def opencode_step_costs(agent_dir):
    """Cross-check only: the sum of the per-request costs OpenCode records (step_finish), which leaves out its title
    request."""
    total = 0.0
    for line in (agent_dir / "transcript.jsonl").read_text(errors="replace").splitlines() if \
            (agent_dir / "transcript.jsonl").exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "step_finish":
            total += (event.get("part") or {}).get("cost") or 0
    return round(total, 8)


PENDING_COSTS = {}  # agent -> threads settling that agent's earlier runs' costs (run(defer_cost=True))


def wait_for_pending_costs(agent=None):
    """Before an OpenCode run reads its agent's key's usage: that agent's earlier runs' usage has settled, so that the
    run's window on the key holds only that run. agent None: wait for all."""
    for a in [agent] if agent else list(PENDING_COSTS):
        threads = PENDING_COSTS.get(a, [])
        while threads:
            thread = threads.pop(0)
            if thread.is_alive():
                log("waiting for %s's previous run's cost to settle on OpenRouter" % a)
            thread.join()


def opencode_token_floor(agent_dir, model):
    """The run's token counts (OpenCode's step_finish events) at OpenRouter's catalogue prices for the model, without
    reasoning tokens, and the prices used. Only a lower bound for settling (OpenCode 2.0.21 logs cost 0 for models
    it does not know, e.g. Claude Haiku 5.5); the cost itself stays the key's usage difference."""
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as response:
            price = next(m["pricing"] for m in json.loads(response.read())["data"] if m["id"] == model)
    except (OSError, ValueError, StopIteration):
        return 0.0, None
    total = 0.0
    for line in (agent_dir / "transcript.jsonl").read_text(errors="replace").splitlines() if \
            (agent_dir / "transcript.jsonl").exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "step_finish":
            t = (event.get("part") or {}).get("tokens") or {}
            cache = t.get("cache") or {}
            total += (t.get("input", 0) * float(price["prompt"]) + t.get("output", 0) * float(price["completion"])
                      + cache.get("read", 0) * float(price.get("input_cache_read", 0))
                      + cache.get("write", 0) * float(price.get("input_cache_write", 0)))
    return round(total, 8), price


def write_opencode_cost(run_dir, key, usage_before, model):
    steps = opencode_step_costs(run_dir / "agent")
    tokens, price = opencode_token_floor(run_dir / "agent", model)
    floor = 0.9 * max(steps, tokens)
    usage_after, waited, settled = settled_usage(key, usage_before, floor)
    (run_dir / "cost.json").write_text(json.dumps({
        "source": "OpenRouter /api/v1/key usage of the agent's own key (agents.json openrouter_key_file), after minus "
                  "before the run",
        "usage_before_usd": usage_before, "usage_after_usd": usage_after,
        "cost_usd": round(usage_after - usage_before, 8), "settled": settled, "waited_s": waited,
        "settle_rule": "unchanged for %d reads %d s apart, after usage - before reached the floor: 0.9 x the larger of "
                       "OpenCode's per-request costs and the token counts at OpenRouter's catalogue prices"
                       % (USAGE_STABLE_READS, USAGE_POLL),
        "settle_floor_usd": round(floor, 8), "cross_check_step_costs_usd": steps,
        "cross_check_tokens_at_catalogue_price_usd": tokens, "catalogue_price_per_token": price}, indent=2))
    log("%s: OpenRouter cost %s" % (run_dir.name, "settled after %d s" % waited if settled else "NOT settled (missing)"))


def settled_usage(key, before=None, floor=0.0):
    """The key's usage once it has stopped changing. OpenRouter adds a run's requests to the key's usage minutes after
    them, and the usage stays unchanged meanwhile (2026-10-15: nothing after 150 s, still incomplete after 7 min), so
    "unchanged" counts only once usage - before has reached floor: the sum of the per-request costs OpenRouter
    returned to OpenCode (opencode_step_costs, which leaves out the title request). Returns (usage, waited s, settled);
    not settled after USAGE_MAX_WAIT s means the cost is recorded as missing."""
    started, reads = time.time(), []
    time.sleep(USAGE_FIRST_READ - USAGE_POLL)
    while time.time() - started < USAGE_MAX_WAIT:
        time.sleep(USAGE_POLL)
        reads.append(openrouter_usage(key))
        reached = before is None or reads[-1] - before >= floor - 1e-9
        if reached and len(reads) >= USAGE_STABLE_READS and len(set(reads[-USAGE_STABLE_READS:])) == 1:
            return reads[-1], round(time.time() - started), True
    return reads[-1], round(time.time() - started), False


# --- one run -------------------------------------------------------------------------------------------------
def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()


def preflight(agent, phase, fake_script):
    """Refuse a run the protocol does not allow (raises SystemExit)."""
    if phase not in records.PHASES:
        raise SystemExit("unknown phase %r" % phase)
    if fake_script and phase != "test":
        raise SystemExit("--fake-llm runs are test runs: phase test only")
    if phase != "test" and not AGENTS[agent].get("model_id_confirmed"):
        raise SystemExit("%s: the model ID %r is not confirmed by the author (pilot/config/agents.json)"
                         % (agent, AGENTS[agent]["model"]))
    open_values = [k for k, v in PROTOCOL.items() if isinstance(v, dict) and v.get("decided") is False]
    if phase == "main" and open_values:
        raise SystemExit("main runs need a frozen protocol; not decided in protocol.json: %s" % ", ".join(open_values))


def run(agent, task, phase="test", slot=None, fake_script=None, test_caps=None, defer_cost=False):
    """One run. slot: the run order's position (pilot/order.py next_slot) for pilot and main runs. defer_cost: settle an
    OpenCode run's cost in a background thread (PENDING_COSTS), so that only the same agent's next run waits for it
    (run_next)."""
    preflight(agent, phase, fake_script)
    if phase != "test" and slot is None:
        raise SystemExit("%s runs come only from the run order (run.py next)" % phase)
    spec, world = AGENTS[agent], WORLDS["worlds"][task]
    prompt = PROTOCOL["prompts"][task]
    caps, grace = dict(PROTOCOL["caps"][task], **(test_caps or {})), PROTOCOL["grace_period_s"]["value"]
    started = time.time()
    run_id = "%s-%s-%s%s" % (task, agent, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                             "-test" if fake_script else "")
    run_dir = records.runs_dir(phase) / run_id
    run_dir.mkdir(parents=True)
    instrumentation = INSTRUMENTATION / run_id
    world_file = REPO / world["file"]
    manifest = {
        "run_id": run_id, "phase": phase, "task": world["task"], "task_key": task, "agent": agent,
        "run_order": {"file": str(records.order_file(phase, task)), "seed": slot["order_seed"],
                      "position": slot["position"], "block": slot["block"], "attempt": slot["attempt"]}
        if slot else None,
        "agent_config": spec, "deviations": AGENTS["deviations"][spec["deviations_ref"]],
        "harness_version": LOCK["harness_versions"][HARNESS_KEY[spec["harness"]]],
        "system_commit": git("rev-parse", "HEAD"), "system_uncommitted_changes": bool(git("status", "--porcelain")),
        "world_file": world["file"], "world_sha256": hashlib.sha256(world_file.read_bytes()).hexdigest(),
        "prompt": prompt, "caps": caps, "grace_period_s": grace,
        "protocol_values_decided": {k: v["decided"] for k, v in PROTOCOL.items() if isinstance(v, dict) and "decided" in v},
        "test": {"fake_llm": True} if fake_script else None,  # completed once the fake LLM has started
    }
    if spec["harness"] in PRICED_MODEL:
        price = PRICES[PRICED_MODEL[spec["harness"]]]
        if datetime.date.today().isoformat() > price["valid_through"]:
            raise SystemExit("pilot/config/prices.json: the %s price is only valid through %s; update it first"
                             % (PRICED_MODEL[spec["harness"]], price["valid_through"]))
        manifest["price"] = dict(price, model=PRICED_MODEL[spec["harness"]])
    log("run %s" % run_id)
    supervisor = None
    test_env = fake_llm_env(spec["harness"]) if fake_script else None
    container = AgentContainer(run_id, agent, run_dir / "agent", bridge_container=None, test_env=test_env,
                               proxy_allow=[r"^no-host-allowed-in-fake-llm-tests\.invalid$"] if fake_script else None)
    end, invalid, times = None, None, {}
    try:
        if spec["harness"] == "agy" and not fake_script and not agy_login_ok(run_id):
            end, invalid = "setup_failure", "the agy login has expired (agy models failed)"
            raise RuntimeError(invalid)
        if spec["harness"] == "opencode" and not fake_script:
            wait_for_pending_costs(agent)  # before the reset, so that the simulator does not idle at the start pose
        supervisor = reset(run_id, world)
        if spec["harness"] == "opencode" and not fake_script:
            container.openrouter_key = openrouter_key(agent)
            times["usage_before_read"] = time.time()
            usage_before = openrouter_usage(container.openrouter_key)
        container.bridge = compose(REPO, "ps", "-q", "bridge")
        container.setup_network()
        if fake_script:
            cap = start_fake_llm(container, fake_script)
            manifest["test"] = {"fake_llm": True, "caps_override": test_caps,
                                "script": json.loads(Path(fake_script).read_text()),
                                "fake_llm_requests": str(cap / "requests.jsonl"),
                                "note": "Fake LLM (pilot/tools/fake_llm.py), fake login, no egress: not a real run."}
        manifest["image"] = {"name": spec["image"], "id": container.create(prompt)}
        if fake_script and spec["harness"] == "agy":
            stage_agy_fake_settings(container)
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
        clear_run_id(run_id)
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
        for name in ("calls.jsonl", "ground_truth.json", "trajectory.jsonl"):
            if (instrumentation / name).exists():
                shutil.copy(instrumentation / name, run_dir / name)
        if container.openrouter_key:
            if defer_cost:
                log("OpenRouter cost: settling in the background (the next OpenCode run waits for it)")
                thread = threading.Thread(target=write_opencode_cost,
                                          args=(run_dir, container.openrouter_key, usage_before, spec["model"]))
                thread.start()
                PENDING_COSTS.setdefault(agent, []).append(thread)
            else:
                log("waiting for OpenRouter's usage of the key to settle")
                write_opencode_cost(run_dir, container.openrouter_key, usage_before, spec["model"])
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
        clear_run_id(run_id)
        container.remove()
        if supervisor is not None:
            stop_supervisor(supervisor)
        manifest["runner_times_mac_clock"] = {k: utc(v) for k, v in dict(times, runner_started=started).items()}
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        log("run folder: %s" % run_dir)
    return run_dir


def run_next(phase, task, fake_script=None, count=1, stop_after_invalid=1):
    """Up to count runs from the run order. An invalid run leaves its position open, so the next run repeats it.
    Stops after stop_after_invalid invalid runs in a row (default 1: the operator sees why first), and at once when
    a usage limit was reached, so that the limit is not hit again right away."""
    import metrics
    done, invalid_in_a_row = [], 0
    for _ in range(count):
        slot = run_order.next_slot(phase, task)
        if slot is None:
            log("every position of the %s order for %s has a valid run" % (phase, task))
            break
        log("position %d (block %d), %s, attempt %d" % (slot["position"], slot["block"], slot["agent"], slot["attempt"]))
        run_dir = run(slot["agent"], task, phase, slot, fake_script, defer_cost=True)
        done.append(run_dir)
        m = metrics.metrics(run_dir)
        if m["valid"] and not m["excluded"]:
            invalid_in_a_row = 0
            continue
        invalid_in_a_row += 1
        log("invalid run (%s); position %d stays open" % ("; ".join(m["invalid_reasons"]) or m["excluded"],
                                                         slot["position"]))
        if (m["provider_failure"] or {}).get("kind") == "usage_limit":
            log("STOP: usage limit reached")
            break
        if invalid_in_a_row >= stop_after_invalid:
            log("STOP: %d invalid run(s) in a row" % invalid_in_a_row)
            break
    wait_for_pending_costs()
    return done


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_next = sub.add_parser("next", help="the next run(s) of a phase's run order")
    p_next.add_argument("phase", choices=records.PHASES)
    p_next.add_argument("task", choices=list(WORLDS["worlds"]))
    p_next.add_argument("--count", type=int, default=1, help="runs in a row")
    p_next.add_argument("--stop-after-invalid", type=int, default=1, metavar="K",
                        help="stop after K invalid runs in a row (each is repeated in place); a usage limit always stops")
    p_next.add_argument("--fake-llm", metavar="SCRIPT", help="test order only: play the stand-in LLM")
    p_run = sub.add_parser("run", help="one test run outside any order (phase test)")
    p_run.add_argument("agent", choices=AGENT_IDS)
    p_run.add_argument("task", choices=list(WORLDS["worlds"]))
    p_run.add_argument("--fake-llm", metavar="SCRIPT", help="test run against pilot/tools/fake_llm.py")
    p_run.add_argument("--test-caps", metavar="NAME=VALUE,...", help="override the task's caps, e.g. tool_calls=3 "
                       "or wall_clock_s=20")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # clean up (container, run ID) when killed
    if args.cmd == "next":
        for path in run_next(args.phase, args.task, args.fake_llm, args.count, args.stop_after_invalid):
            print(path)
    else:
        test_caps = None
        if args.test_caps:
            test_caps = {k: int(v) for k, v in (item.split("=") for item in args.test_caps.split(","))}
            unknown = set(test_caps) - set(PROTOCOL["caps"][args.task])
            if unknown:
                parser.error("unknown caps: %s" % ", ".join(sorted(unknown)))
        print(run(args.agent, args.task, "test", None, args.fake_llm, test_caps))
