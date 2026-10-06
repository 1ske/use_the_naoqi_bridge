#!/usr/bin/env python
"""HTTP/JSON bridge from the MCP server to NAOqi 2.1.4: safe movement primitives only.

    POST /call/<primitive>  body: JSON object of arguments
        200 {"ok": true, "result": {..., "battery": {"percent", "charging"}}}
        4xx/5xx {"ok": false, "error": "...", "state": {...}, "battery": {...}}  (when they could be read)
    POST /heartbeat         keeps the client lease alive (the MCP server sends it every few s)
    GET  /health            NAOqi connection, protections, current movement

The primitives, their arguments and their kind (read / move / override) are listed in
primitives.json. Each one has exactly one handler below (@primitive). The bridge owns safety
and trusts none of the arguments it receives:
- arguments are validated against primitives.json, walks are clamped (1.0 m, pi rad per call),
  posture speed <= MAX_POSTURE_SPEED
- walks use ALMotion.moveTo with NAOqi's external collision protection for "Move" turned on (on every
  (re)connect, and kept on): NAOqi itself refuses or stops a walk toward an obstacle in its security
  area (sonar, fed by ALNavigation's safety loop); a walk that ends short of what was commanded is
  reported as `obstacle`. Walking is refused while that protection isn't running. Walks also stop
  when a foot bumper gets pressed
- `go` turns, then walks straight up to GO_MAX_DISTANCE m
- camera images: every successful result carries one JPEG with the top view above the bottom view
  ("images", --auto-image) and ground-distance lines (--image-grid); `look` grabs it without moving,
  `look_around` turns the head (clamped, joint speed <= MAX_JOINT_SPEED) and returns one panorama;
  a missing camera only warns
- one movement at a time; rest always runs and interrupts it
- walking needs an awake, standing robot, finite odometry and no unresolved fall
- a fall stops the current movement and blocks walking until stand succeeds
- losing the client (no request for --client-timeout s, or the caller hangs up during a
  movement) and shutting down (SIGTERM/SIGINT) stop movement and put the robot in rest
- the fall manager, foot-contact and self-collision protections are never touched; the external
  collision protection for Move is only ever turned on
- NAOqi's own behaviours (autonomous life, basic awareness = face tracking, autonomous moves,
  breathing / idle posture) are turned off on every (re)connect and kept off, so the robot only
  moves when told to; the bridge leaves them off when it exits
"""
from __future__ import print_function

import base64
import io
import json
import math
import os
import select
import signal
import socket
import sys
import threading
import time
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from SocketServer import ThreadingMixIn

from PIL import Image, ImageDraw

from common import MAX_JOINT_SPEED, MAX_POSTURE_SPEED, clamp, end_limp, parse_args, proxies

MAX_WALK_DISTANCE = 1.0  # m per walk_to
MAX_WALK_TURN = math.pi  # rad per walk_to
POSTURE_TIMEOUT = 30.0   # s
POLL = 0.1               # s, while waiting for a movement
MONITOR_PERIOD = 0.2     # s, fall and client-lease checks
SONAR_NAME = "use_the_naoqi_bridge"
CAMERA_NAME = "use_the_naoqi_bridge_camera"
CAMERAS = {"top": 0, "bottom": 1}   # kTopCamera, kBottomCamera
K_QVGA, K_RGB, CAMERA_FPS = 1, 11, 5  # naoqisim only fills QVGA buffers (VGA comes back solid green)
JPEG_QUALITY = 90
GRID_M = {"top": (0.5, 1.0, 1.5, 2.0, 3.0), "bottom": (0.25, 0.5, 0.75, 1.0)}
FRAME_ROBOT = 2
HEAD_JOINTS = ["HeadYaw", "HeadPitch"]
HEAD_YAW_MAX = 2.0                  # rad (joint limit 2.09)
HEAD_PITCH_MIN, HEAD_PITCH_MAX = -0.6, 0.5  # rad, positive looks down (joint limits -0.67 / 0.51)
HEAD_TIMEOUT = 3.0                  # s to reach a head angle
FRAME_WAIT = 0.3                    # s after the head stops, so the next frame (5 fps) shows the new view
SWEEP_YAWS = (1.6, 0.8, 0.0, -0.8, -1.6)  # rad; 61 deg FOV each, so about +-150 deg in total
GO_MAX_DISTANCE = 3.0               # m per go
SHORT_M, SHORT_RAD = 0.05, 0.1      # a completed walk this much short of the command was stopped by NAOqi
MAX_SAY_CHARS = 300                 # per say call
HFOV = 1.064                        # rad, NAO V5 cameras (Webots Nao.proto uses the same)
AUTONOMY_MODULES = ("ALAutonomousLife", "ALBasicAwareness", "ALAutonomousMoves")
AUTONOMY_CHECK_PERIOD = 2.0         # s between the monitor's checks that autonomy stayed off

D = "Device/SubDeviceList/"
BATTERY_KEY = D + "Battery/Charge/Sensor/Value"  # 0..1; ALBattery.getBatteryCharge() is 0 in naoqisim
BATTERY_CURRENT_KEY = D + "Battery/Current/Sensor/Value"  # A, positive while charging
SONAR_KEYS = [D + "US/Left/Sensor/Value", D + "US/Right/Sensor/Value"]
TILT_KEYS = [D + "InertialSensor/AngleX/Sensor/Value", D + "InertialSensor/AngleY/Sensor/Value"]
BUMPER_KEYS = [D + "LFoot/Bumper/Left/Sensor/Value", D + "LFoot/Bumper/Right/Sensor/Value",
               D + "RFoot/Bumper/Left/Sensor/Value", D + "RFoot/Bumper/Right/Sensor/Value"]
FALLEN_FAMILIES = ("LyingBack", "LyingBelly", "LyingLeft", "LyingRight", "Back", "Belly", "Left", "Right")
NAN_ODOMETRY = ("ALMotion odometry is NaN (seen in naoqisim after emulation stalls, NOTES.md): "
                "walks would silently do nothing. Restart naoqisim.")
NO_IMAGE = ("no camera image: naoqisim started with -nocam, or ALVideoDevice unavailable "
            "(see the bridge log)")
GET_UP_HINT = ("Getting up after a fall is unreliable (naoqisim: only from the back and only at "
               "speed 1.0, above the 0.5 cap). Call rest and ask a human to reset the robot.")

REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "primitives.json")
HANDLERS = {}


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message))
    sys.stdout.flush()


class CallLog(object):
    """Per-run record of tool calls and falls, for the evaluation's instrumentation (off by default).

    Active only when BRIDGE_RUN_DIR is set (a host directory that only the bridge container mounts) and
    holds a file `run_id` written by the runner; without a run ID nothing is written anywhere. Each tool
    call becomes one line of <dir>/<run_id>/calls.jsonl: times received and returned, tool, arguments as
    received, and the result (without image pixels) or the error type. Falls get their own lines. The
    run's first `rest` also creates <dir>/<run_id>/rest_received as soon as it arrives, before the robot
    stops or crouches, so the simulator's ground-truth supervisor can take the pose at that moment."""

    OUTCOMES = {400: "rejected", 404: "rejected", 409: "refused", 502: "naoqi_error", 503: "unreachable"}

    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.Lock()

    def run(self):
        if not self.directory:
            return None
        try:
            with open(os.path.join(self.directory, "run_id")) as f:
                run_id = f.read().strip()
        except IOError:
            return None
        return run_id if run_id and all(c.isalnum() or c in "-_." for c in run_id) else None

    def _path(self, run_id, name):
        folder = os.path.join(self.directory, run_id)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        return os.path.join(folder, name)

    def rest_received(self, run_id, received):
        if run_id:
            try:
                fd = os.open(self._path(run_id, "rest_received"), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                os.write(fd, "%.6f\n" % received)
                os.close(fd)
            except OSError:
                pass  # not the run's first rest

    def write(self, run_id, record):
        if not run_id:
            return
        line = json.dumps(record, sort_keys=True) + "\n"
        with self.lock:
            with open(self._path(run_id, "calls.jsonl"), "a") as f:
                f.write(line)

    @staticmethod
    def without_pixels(result):
        strip = lambda frame: dict((k, v) for k, v in frame.items() if k != "data")
        result = dict(result)
        if "image" in result:
            result["image"] = strip(result["image"])
        if "images" in result:
            result["images"] = [strip(frame) for frame in result["images"]]
        return result


class Refused(Exception):
    """A safety rule refused the command (HTTP 409)."""


class BadArgs(Exception):
    """Arguments don't match primitives.json (HTTP 400)."""


def naoqi_message(e):
    lines = [line.strip() for line in str(e).splitlines() if line.strip()]
    return ": ".join(lines[-2:]) if lines else repr(e)


def is_nan(values):
    return any(v is None or math.isnan(v) for v in values)


def rounded(value, digits=3):
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def pose(values):
    x, y, theta = values
    return {"x": rounded(x), "y": rounded(y), "theta": rounded(theta)}


# ---------------------------------------------------------------------------- shared state

class Robot(object):
    """NAOqi proxies for one thread, created on first use and again after NAOqi restarts."""

    def __init__(self, args, sonar=False, quiet=False):
        self.args = args
        self.sonar = sonar
        self.quiet = quiet  # turn NAOqi's own behaviours off on every (re)connect
        self.p = None
        self.ap = {}  # the autonomy modules this NAOqi has

    def get(self):
        if self.p is None:
            p = proxies(self.args, "ALMotion", "ALRobotPosture", "ALMemory")
            if self.sonar:  # sonar values only update while someone is subscribed
                try:
                    proxies(self.args, "ALSonar")["ALSonar"].subscribe(SONAR_NAME)
                except RuntimeError as e:
                    log("WARNING: no sonar: %s" % naoqi_message(e))
            for optional in ("ALNavigation", "ALTextToSpeech"):  # ALNavigation: obstacle protection status
                try:
                    p.update(proxies(self.args, optional))
                except RuntimeError:
                    pass
            self.ap = autonomy_proxies(self.args)
            if self.quiet:
                log("autonomy off: %s" % json.dumps(quiet_autonomy(self.ap, p["ALMotion"])))
                collision_protection_on(p["ALMotion"])
            self.p = p
        return self.p

    def check(self):
        """After a NAOqi error: drop the proxies if NAOqi itself is gone, so the next call reconnects.
        Returns True if they were dropped."""
        try:
            if self.p is not None:
                self.p["ALMotion"].ping()
            return False
        except Exception:
            log("lost NAOqi, will reconnect")
            self.p = None
            camera.reset()  # a restarted NAOqi has none of our subscriptions
            return True


class Movement(object):
    """The single movement slot. rest (and falls, lost clients) set stop_requested; the running move sees it and ends."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = None
        self.reason = None
        self.hold = False  # set while rest runs, so no new move can slip in
        self.stop_requested = threading.Event()

    def begin(self, name):
        if self.hold:
            raise Refused("rest is in progress; wait for it, then call stand")
        if not self._lock.acquire(False):
            raise Refused("busy: %s is still running (one movement at a time). Wait for it to "
                          "finish, or call rest to interrupt it." % self.current)
        self.current = name
        self.reason = None
        self.stop_requested.clear()

    def acquire(self, name, timeout):
        """Wait up to timeout s for the slot (used by rest after interrupting). Returns True if owned."""
        deadline = time.time() + timeout
        while not self._lock.acquire(False):
            if time.time() > deadline:
                return False
            time.sleep(0.05)
        self.current = name
        return True

    def end(self):
        self.current = None
        self._lock.release()

    def interrupt(self, p, reason):
        self.reason = reason
        self.stop_requested.set()
        for module in ("ALMotion", "ALRobotPosture"):
            try:
                p[module].stopMove()
            except Exception as e:
                log("WARNING: %s.stopMove failed: %s" % (module, naoqi_message(e)))


class FallGuard(object):
    """Sticky fall flag: set by the monitor, cleared only by a successful stand."""

    def __init__(self):
        self.fallen = False
        self.count = 0
        self.last = None

    def note(self, reason):
        if not self.fallen:
            self.count += 1
            self.last = {"reason": reason, "time": time.strftime("%H:%M:%S")}
            log("FALL detected (%s): stopping, walking blocked until stand succeeds" % reason)
        self.fallen = True

    def clear(self):
        if self.fallen:
            log("fall cleared: robot is standing again")
        self.fallen = False


class Lease(object):
    """The client counts as present while it sends requests at least every `timeout` s."""

    def __init__(self, timeout):
        self.timeout = timeout
        self.last = time.time()
        self.fired = False

    def touch(self):
        self.last = time.time()
        self.fired = False

    def expired(self):
        return self.timeout > 0 and not self.fired and time.time() - self.last > self.timeout


def upright(pose, tilt):
    """The camera pose in FRAME_ROBOT comes from the joints alone, assuming flat feet: only trust it
    (ground lines, free-space measurements) when the IMU (tilt = torso roll, pitch) agrees."""
    return not (guard.fallen or pose[2] < 0.2 or abs(pose[3]) > 0.2 or abs(tilt[0]) > 0.25
                or abs(tilt[1]) > 0.35)


def grid_row(distance, size, pose):
    """Image row of the floor `distance` m ahead of the camera (pose in FRAME_ROBOT), straight ahead."""
    width, height = size
    focal = (width / 2.0) / math.tan(HFOV / 2)
    return height / 2.0 + focal * math.tan(math.atan2(pose[2], distance) - pose[4])


def draw_grid(image, distances, pose):
    """Draw a line where the ground is d m ahead of the camera, for each d. Returns the distances drawn."""
    width, rows = image.size
    draw = ImageDraw.Draw(image)
    drawn = []
    for distance in distances:
        row = grid_row(distance, image.size, pose)
        if not 0 <= row < rows:
            continue
        draw.line([(0, row), (width, row)], fill=(255, 230, 0))
        label = "%gm" % distance
        draw.text((3, row - 11), label, fill=(0, 0, 0))
        draw.text((2, row - 12), label, fill=(255, 230, 0))
        drawn.append(distance)
    return drawn


class Frame(object):
    """One camera image (PIL, no drawing on it) with the camera pose in FRAME_ROBOT when it was taken."""

    def __init__(self, camera, image, pose, head, upright):
        self.camera, self.image, self.pose, self.head, self.upright = camera, image, pose, head, upright

    def tile(self, grid):
        """(a copy with the ground lines drawn, metadata)."""
        image = self.image.copy()
        meta = {"camera": self.camera, "direction": direction(self.head[0]),
                "head": {"yaw": rounded(self.head[0]), "pitch": rounded(self.head[1])}}
        if grid and self.upright:
            meta["grid_m"] = draw_grid(image, GRID_M[self.camera], self.pose)
        return image, meta


def compose(rows, grid):
    """One JPEG view from rows of Frames (all the same size): {"layout", "tiles", "mime_type", "data"}.
    Each tile is labelled with its camera and direction."""
    width, height = rows[0][0].image.size
    sheet = Image.new("RGB", (width * max(len(row) for row in rows), height * len(rows)))
    draw = ImageDraw.Draw(sheet)
    tiles = []
    for r, row in enumerate(rows):
        for c, frame in enumerate(row):
            image, meta = frame.tile(grid)
            sheet.paste(image, (c * width, r * height))
            label = "%s %s" % (frame.camera, meta["direction"])
            draw.rectangle([c * width, r * height, c * width + 7 * len(label) + 4, r * height + 12], fill=(0, 0, 0))
            draw.text((c * width + 2, r * height + 1), label, fill=(255, 255, 255))
            if c:
                draw.line([(c * width, r * height), (c * width, (r + 1) * height)], fill=(0, 0, 0))
            tiles.append(meta)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=JPEG_QUALITY)
    view = {"layout": "%d x %d tiles of %dx%d px, left to right = left to right"
                      % (len(rows), max(len(row) for row in rows), width, height),
            "tiles": tiles, "mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue())}
    if any(meta.get("grid_m") for meta in tiles):
        view["grid"] = "yellow lines: ground this far (m) ahead of the camera"
    return view


class Camera(object):
    """Head camera frames as JPEG. Subscriptions stay open between calls (a fresh one can hand back
    a stale or empty first frame). Best effort: frame() returns None instead of raising."""

    def __init__(self, args):
        self.args = args
        self._lock = threading.Lock()  # the HTTP server is threaded
        self.video = None
        self.handles = {}
        self.last_error = None

    def _handle(self, name):
        if self.video is None:
            self.video = proxies(self.args, "ALVideoDevice")["ALVideoDevice"]
            for old in self.video.getSubscribers():  # left over from a bridge that didn't shut down cleanly
                if old.startswith(CAMERA_NAME):
                    self.video.unsubscribe(old)
        if name not in self.handles:
            self.handles[name] = self.video.subscribeCamera(
                "%s_%s" % (CAMERA_NAME, name), CAMERAS[name], K_QVGA, K_RGB, CAMERA_FPS)
            if self.last_error is None:  # quiet while a missing camera makes us resubscribe every call
                log("camera %s subscribed (%s)" % (name, self.handles[name]))
        return self.handles[name]

    def frame(self, p, name):
        """A Frame from camera `name` ("top" / "bottom"), or None."""
        try:
            with self._lock:
                handle = self._handle(name)
                raw = self.video.getImageRemote(handle)
            if not raw or not raw[0] or not raw[6]:
                raise RuntimeError("empty frame")
            width, height, data = raw[0], raw[1], raw[6]
            if data == data[:3] * (len(data) // 3):  # naoqisim -nocam (or VGA) hands back solid green
                raise RuntimeError("blank frame (one colour): naoqisim is probably running with -nocam")
            motion = p["ALMotion"]
            pose = motion.getPosition("Camera" + name.capitalize(), FRAME_ROBOT, True)
            frame = Frame(name, Image.frombytes("RGB", (width, height), bytes(data)), pose,
                          motion.getAngles(HEAD_JOINTS, True), upright(pose, p["ALMemory"].getListData(TILT_KEYS)))
            self.last_error = None
            return frame
        except Exception as e:
            message = naoqi_message(e)
            if message != self.last_error:  # don't repeat it on every call
                log("WARNING: camera %s: %s" % (name, message))
                self.last_error = message
            self.close()  # stale handles after a NAOqi restart: subscribe again next time
            self.reset()
            return None

    def reset(self):
        """Forget proxy and handles (after NAOqi went away, the old ones are meaningless)."""
        with self._lock:
            self.video, self.handles = None, {}

    def close(self):
        with self._lock:
            for handle in self.handles.values():
                try:
                    self.video.unsubscribe(handle)
                except Exception:
                    pass  # NAOqi gone or handle already invalid
            self.handles = {}


movement = Movement()
guard = FallGuard()
lease = Lease(0)  # replaced in main() with --client-timeout
camera = Camera(None)  # replaced in main() with the NAOqi address
move_config = []  # replaced in main() with --max-step-x


def read_state(p):
    motion, posture, memory = p["ALMotion"], p["ALRobotPosture"], p["ALMemory"]
    odometry = motion.getRobotPosition(True)
    family = posture.getPostureFamily()
    values = memory.getListData(SONAR_KEYS + BUMPER_KEYS)
    sonar_left, sonar_right = values[:2]
    bumpers = [bool(v and v > 0.5) for v in values[2:]]
    warnings = []
    if is_nan(odometry):
        warnings.append(NAN_ODOMETRY)
    if guard.fallen:
        warnings.append("The robot fell: walking is refused until stand succeeds.")
    return {
        "posture": posture.getPosture(),
        "posture_family": family,
        "fallen": guard.fallen or family in FALLEN_FAMILIES,
        "walk_blocked_by_fall": guard.fallen,
        "awake": motion.robotIsWakeUp(),
        "odometry": None if is_nan(odometry) else pose(odometry),
        "sonar_m": {"left": rounded(sonar_left, 2), "right": rounded(sonar_right, 2)},
        "bumpers": {"left_foot": bumpers[0] or bumpers[1], "right_foot": bumpers[2] or bumpers[3]},
        "busy": movement.current,
        "fall_count": guard.count,
        "last_fall": guard.last,
        "warnings": warnings,
    }


def read_battery(p):
    """{"percent", "charging"} for every reply, or None when NAOqi can't be read."""
    try:
        charge, current = p["ALMemory"].getListData([BATTERY_KEY, BATTERY_CURRENT_KEY])
    except Exception:
        return None
    return {"percent": None if charge is None else rounded(charge * 100, 0),
            "charging": bool(current and current > 0.05)}


def view_of(p, names=("top", "bottom")):
    """(one composed view of the named cameras stacked top to bottom or None, all frames there)."""
    frames = [frame for frame in (camera.frame(p, name) for name in names) if frame]
    return (compose([[frame] for frame in frames], camera.args.image_grid) if frames else None,
            len(frames) == len(names))


def attach_view(p, result):
    """Add the top view above the bottom view to a result, and a warning if a camera is missing."""
    view, complete = view_of(p)
    if view:
        result["images"] = [view]
    if not complete:
        result.setdefault("warnings", []).append(NO_IMAGE)


def pressed_bumpers(memory):
    return set(key for key, v in zip(BUMPER_KEYS, memory.getListData(BUMPER_KEYS)) if v and v > 0.5)


def protections(p):
    motion = p["ALMotion"]
    return {
        "fall_manager": motion.getFallManagerEnabled(),
        "external_collision_move": motion.getExternalCollisionProtectionEnabled("Move"),
        "external_collision_arms": motion.getExternalCollisionProtectionEnabled("Arms"),
        "self_collision_arms": motion.getCollisionProtectionEnabled("Arms"),
    }


def collision_protection_on(motion):
    """NAOqi's own obstacle stop for walks (ALMotion external collision protection, "Move")."""
    attempt("ALMotion.setExternalCollisionProtectionEnabled", motion.setExternalCollisionProtectionEnabled,
            "Move", True)


def start_obstacle_protection(p):
    """Make sure NAOqi's obstacle protection is running; the robot must be awake. ALNavigation's safety
    loop, which feeds the protection its sonar obstacles, only starts when the protection is turned on
    while the robot is awake (NAOqi 2.1.4; turning it on again when it's already on does nothing), so
    turn it off and on while the robot stands still. Returns obstacle_protection(p)."""
    status = obstacle_protection(p)
    if status["running"] is False:
        log("NAOqi's obstacle protection isn't running, restarting it")
        motion = p["ALMotion"]
        attempt("ALMotion.setExternalCollisionProtectionEnabled", motion.setExternalCollisionProtectionEnabled,
                "Move", False)
        collision_protection_on(motion)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.2)
            status = obstacle_protection(p)
            if status["running"]:
                break
    elif not status["enabled"]:
        collision_protection_on(p["ALMotion"])
        status = obstacle_protection(p)
    return status


def obstacle_protection(p):
    """{"enabled": NAOqi's Move collision protection, "running": its obstacle feed (ALNavigation's safety
    loop), None without ALNavigation}."""
    running = None
    if "ALNavigation" in p:
        try:
            running = p["ALNavigation"]._isSafetyLoopRunning()
        except RuntimeError:
            running = None
    return {"enabled": p["ALMotion"].getExternalCollisionProtectionEnabled("Move"), "running": running}


def autonomy_proxies(args):
    """{name: ALProxy} for the AUTONOMY_MODULES this NAOqi has (naoqisim may lack some)."""
    found = {}
    for name in AUTONOMY_MODULES:
        try:
            found.update(proxies(args, name))
        except RuntimeError:
            pass
    return found


def attempt(what, fn, *args):
    """Call fn; on any error (missing module or method on this NAOqi) only warn."""
    try:
        return fn(*args)
    except Exception as e:
        log("WARNING: %s failed: %s" % (what, naoqi_message(e)))
        return None


def quiet_motion(motion):
    """Breathing and idle posture off (wakeUp may turn them back on)."""
    for method in ("setBreathEnabled", "setIdlePostureEnabled"):
        attempt("ALMotion.%s" % method, lambda m=method: getattr(motion, m)("Body", False))


def quiet_autonomy(ap, motion):
    """Turn off NAOqi's own behaviours so the robot only moves when the bridge tells it:
    autonomous life (disabling it rests the robot), basic awareness (face / sound tracking),
    autonomous moves, breathing and idle posture. Returns autonomy_status."""
    for module, method, args in (("ALAutonomousLife", "setState", ("disabled",)),
                                 ("ALBasicAwareness", "stopAwareness", ()),
                                 ("ALAutonomousMoves", "setExpressiveListeningEnabled", (False,)),
                                 ("ALAutonomousMoves", "setBackgroundStrategy", ("none",))):
        if module in ap:
            attempt("%s.%s" % (module, method), lambda: getattr(ap[module], method)(*args))
    quiet_motion(motion)
    return autonomy_status(ap, motion)


def autonomy_status(ap, motion):
    """What of NAOqi's own behaviours is on; None = not available on this NAOqi."""
    def read(proxy, method, *args):
        try:
            return getattr(proxy, method)(*args)
        except Exception:
            return None
    return {
        "autonomous_life": read(ap.get("ALAutonomousLife"), "getState"),
        "basic_awareness": read(ap.get("ALBasicAwareness"), "isAwarenessRunning"),
        "expressive_listening": read(ap.get("ALAutonomousMoves"), "getExpressiveListeningEnabled"),
        "background_strategy": read(ap.get("ALAutonomousMoves"), "getBackgroundStrategy"),
        "breath": read(motion, "getBreathEnabled", "Body"),
        "idle_posture": read(motion, "getIdlePostureEnabled", "Body"),
    }


def autonomy_back(status):
    """The behaviours in status that would move the robot on their own (ignoring breathing)."""
    back = []
    if status["autonomous_life"] not in (None, "disabled"):
        back.append("autonomous life %s" % status["autonomous_life"])
    if status["basic_awareness"]:
        back.append("basic awareness")
    if status["expressive_listening"]:
        back.append("expressive listening")
    if status["background_strategy"] not in (None, "none"):
        back.append("background strategy %s" % status["background_strategy"])
    return back


def stop_and_rest(p, reason):
    """Interrupt any movement, then end limp (safe_session's exit, see common.end_limp)."""
    log("stop and rest (%s)" % reason)
    movement.hold = True
    try:
        movement.interrupt(p, "rested")
        owned = movement.acquire("rest", timeout=5.0)
        if not owned:
            log("WARNING: movement %s did not end within 5 s, resting anyway" % movement.current)
        try:
            return end_limp(p["ALMotion"])
        finally:
            if owned:
                movement.end()
    finally:
        movement.hold = False


# ---------------------------------------------------------------------------- primitives

class Call(object):
    """What a handler gets: proxies, validated arguments, and a check for a vanished caller."""

    def __init__(self, p, args, connection):
        self.p = p
        self.args = args
        self.connection = connection

    def client_gone(self):
        if self.connection is None:
            return False
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == ""
        except socket.error:
            return True


def primitive(name):
    def register(handler):
        HANDLERS[name] = handler
        return handler
    return register


def wait_task(call, proxy, task, timeout, check=None):
    """Wait for a .post task. Returns completed / stopped / rested / fell / timeout / client_lost,
    or whatever `check()` returns when it returns something (it then stops the task)."""
    deadline = time.time() + timeout
    falls = guard.count
    outcome = "completed"
    while proxy.isRunning(task):
        if movement.stop_requested.is_set():
            outcome = movement.reason or "stopped"
            break
        reason = check() if check else None
        if reason:
            log("%s stopped: %s" % (movement.current, reason))
            movement.interrupt(call.p, reason)
            outcome = reason
            break
        if call.client_gone():
            log("client hung up during %s" % movement.current)
            movement.interrupt(call.p, "client_lost")
            outcome = "client_lost"
            break
        lease.touch()  # the caller is still connected and waiting
        if time.time() > deadline:
            movement.interrupt(call.p, "timeout")
            outcome = "timeout"
            break
        time.sleep(POLL)
    if outcome == "completed" and movement.stop_requested.is_set():
        outcome = movement.reason or "stopped"  # stopMove ended the task before the loop saw the request
    if outcome != "completed":
        proxy.wait(task, 5000)  # let the interrupted task wind down
    if guard.count != falls:
        outcome = "fell"
    if outcome == "client_lost":
        end_limp(call.p["ALMotion"])  # we still own the movement slot, so rest here
    return outcome


def go_to_posture(call, name):
    speed = clamp(MAX_POSTURE_SPEED, MAX_POSTURE_SPEED)
    task = call.p["ALRobotPosture"].post.goToPosture(name, speed)
    return wait_task(call, call.p["ALRobotPosture"], task, POSTURE_TIMEOUT)


@primitive("get_state")
def get_state(call):
    return read_state(call.p)


@primitive("look")
def look(call):
    which = call.args.get("camera", "both")
    view, complete = view_of(call.p, ("top", "bottom") if which == "both" else (which,))
    result = {"images": [view] if view else [], "state": read_state(call.p)}
    if not complete:
        result["warnings"] = [NO_IMAGE]
    return result


def direction(yaw):
    degrees = int(round(math.degrees(abs(yaw))))
    return "ahead" if degrees < 3 else "%s %d deg" % ("left" if yaw > 0 else "right", degrees)


def turn_head(call, yaw, pitch):
    """Move the head at the capped joint speed and wait until it's there, or has stopped short (NAOqi
    limits the pitch when the head is turned far). False if interrupted."""
    motion = call.p["ALMotion"]
    motion.setAngles(HEAD_JOINTS, [yaw, pitch], clamp(MAX_JOINT_SPEED, MAX_JOINT_SPEED))
    deadline = time.time() + HEAD_TIMEOUT
    last = None
    while time.time() < deadline:
        if movement.stop_requested.is_set():
            return False
        now_yaw, now_pitch = motion.getAngles(HEAD_JOINTS, True)
        if abs(now_yaw - yaw) < 0.03 and abs(now_pitch - pitch) < 0.03:
            break
        if last and abs(now_yaw - yaw) < 0.03 and abs(now_pitch - last[1]) < 0.003:
            break  # yaw reached, pitch stuck at NAOqi's limit for this yaw
        last = (now_yaw, now_pitch)
        time.sleep(POLL)
    time.sleep(FRAME_WAIT)
    return not movement.stop_requested.is_set()


def sweep(call, yaws, pitch=0.0):
    """Turn the head to each yaw and grab top + bottom Frames there; face forward again after.
    Returns (outcome, [[top, bottom] per yaw that was reached], missing frame count)."""
    motion = call.p["ALMotion"]
    if guard.fallen:
        raise Refused("the robot fell: call stand first (or rest)")
    if not motion.robotIsWakeUp():
        raise Refused("the robot is not awake (head motors off): call stand first")
    pitch = max(HEAD_PITCH_MIN, min(HEAD_PITCH_MAX, pitch))
    columns, missing, outcome = [], 0, "completed"
    try:
        for yaw in yaws:
            if not turn_head(call, max(-HEAD_YAW_MAX, min(HEAD_YAW_MAX, yaw)), pitch):
                outcome = movement.reason or "stopped"
                break
            frames = [camera.frame(call.p, name) for name in ("top", "bottom")]
            missing += frames.count(None)
            columns.append(frames)
    finally:
        if movement.reason not in ("rested", "fell"):
            turn_head(call, 0.0, 0.0)  # face forward again, so walks and auto images look ahead
    return outcome, columns, missing


def panorama(columns):
    """One view: the top frames left to right in the first row, the bottom frames below them."""
    rows = [[frames[i] for frames in columns if frames[i]] for i in (0, 1)]
    rows = [row for row in rows if row]
    return [compose(rows, camera.args.image_grid)] if rows else []


@primitive("look_around")
def look_around(call):
    yaws = [call.args["yaw"]] if "yaw" in call.args else SWEEP_YAWS
    outcome, columns, missing = sweep(call, yaws, call.args.get("pitch", 0.0))
    result = {"result": outcome, "images": panorama(columns), "state": read_state(call.p)}
    if missing and outcome == "completed":
        result["warnings"] = [NO_IMAGE]
    return result


def not_awake_hint(p):
    """Why wakeUp left the motors off: joints with an active diagnosis error stay limp
    (ALMotion diagnosis effect), e.g. all of them when the motor boards don't answer."""
    try:
        joints = p["ALMotion"].getBodyNames("Body")
        errors = p["ALMemory"].getListData(["Diagnosis/Active/%s/Error" % j for j in joints])
        flagged = [j for j, e in zip(joints, errors) if e]
    except RuntimeError:
        flagged = []
    hint = "wakeUp left the motors off. "
    if flagged:
        hint += "NAOqi reports active diagnosis errors on %d joints (%s). " % (
            len(flagged), ", ".join(flagged))
    return hint + "Tell the human: the robot needs checking, e.g. a reboot (hold the chest button)."


@primitive("stand")
def stand(call):
    was_fallen = guard.fallen
    call.p["ALMotion"].wakeUp()  # on the H25 this only stiffens; after a fall it restores stiffness
    if not call.p["ALMotion"].robotIsWakeUp():
        return {"result": "failed", "state": read_state(call.p), "hint": not_awake_hint(call.p)}
    quiet_motion(call.p["ALMotion"])
    outcome = go_to_posture(call, "StandInit")
    state = read_state(call.p)
    if (outcome == "completed" and state["awake"] and state["posture_family"] == "Standing"
            and state["posture"] != "Crouch"):  # NAOqi 2.1 puts Crouch in the Standing family
        guard.clear()
        state = read_state(call.p)
    elif outcome == "completed":
        outcome = "failed"
    result = {"result": outcome, "state": state}
    if outcome != "completed" and was_fallen:
        result["hint"] = GET_UP_HINT
    return result


def check_can_walk(call):
    """Refuse unless the robot can walk; returns the odometry to measure the walk from."""
    motion, posture = call.p["ALMotion"], call.p["ALRobotPosture"]
    if guard.fallen:
        raise Refused("the robot fell (%s): walking is blocked until stand succeeds"
                      % guard.last["reason"])
    if not motion.robotIsWakeUp():
        raise Refused("the robot is not awake: call stand first")
    family, name = posture.getPostureFamily(), posture.getPosture()
    if family != "Standing" or name == "Crouch":  # NAOqi 2.1 puts Crouch in the Standing family
        raise Refused("the robot is not standing (posture %s, family %s): call stand first" % (name, family))
    protection = start_obstacle_protection(call.p)
    if protection["running"] is None:
        raise Refused("this NAOqi has no ALNavigation, so it can't stop walks at obstacles: walking is "
                      "blocked. Tell the human.")
    if not protection["running"]:
        raise Refused("NAOqi's obstacle protection doesn't start (ALNavigation's safety loop): walking is "
                      "blocked. Call rest and tell the human.")
    before = motion.getRobotPosition(True)
    if is_nan(before):
        raise Refused(NAN_ODOMETRY)
    return before


def walk_guard(memory, stopped_by):
    """wait_task check for walks: a foot bumper that gets pressed during the walk (one already pressed
    at the start, e.g. a foot against a wall, doesn't block walking away)."""
    pressed_before = pressed_bumpers(memory)
    def check():
        if pressed_bumpers(memory) - pressed_before:
            stopped_by["bumper"] = True
            return "bumped"
        return None
    return check


def walk_leg(call, x, y, theta):
    """One ALMotion.moveTo with the bumper guard; NAOqi's collision protection refuses or stops walks
    toward obstacles. Returns (outcome, stopped_by or None); a completed walk that ended short of the
    command is "obstacle"."""
    motion, memory = call.p["ALMotion"], call.p["ALMemory"]
    stopped_by = {}
    before = motion.getRobotPosition(True)
    motion.moveInit()
    task = motion.post.moveTo(x, y, theta, move_config)
    timeout = 20.0 + 20.0 * math.hypot(x, y) + 10.0 * abs(theta)
    outcome = wait_task(call, motion, task, timeout, check=walk_guard(memory, stopped_by))
    delta = odometry_delta(before, motion.getRobotPosition(True))
    if outcome == "completed" and delta and (
            math.hypot(x - delta["x"], y - delta["y"]) > SHORT_M or abs(theta - delta["theta"]) > SHORT_RAD):
        outcome = "obstacle"
        stopped_by["naoqi"] = "collision protection stopped the walk (obstacle in its security area)"
    return outcome, stopped_by or None


def odometry_delta(before, after):
    """after - before in the robot frame at `before`, or None when odometry is NaN."""
    if is_nan(after):
        return None
    dx, dy = after[0] - before[0], after[1] - before[1]
    c, s = math.cos(before[2]), math.sin(before[2])
    dtheta = math.atan2(math.sin(after[2] - before[2]), math.cos(after[2] - before[2]))
    return {"x": rounded(c * dx + s * dy), "y": rounded(-s * dx + c * dy), "theta": rounded(dtheta)}


@primitive("walk_to")
def walk_to(call):
    motion = call.p["ALMotion"]
    x, y, theta = call.args["x"], call.args["y"], call.args["theta"]
    clamped = []
    distance = math.hypot(x, y)
    if distance > MAX_WALK_DISTANCE:
        x, y = x * MAX_WALK_DISTANCE / distance, y * MAX_WALK_DISTANCE / distance
        clamped.append("distance %.2f m clamped to %.1f m" % (distance, MAX_WALK_DISTANCE))
    if abs(theta) > MAX_WALK_TURN:
        clamped.append("turn %.2f rad clamped to pi" % theta)
        theta = math.copysign(MAX_WALK_TURN, theta)

    before = check_can_walk(call)
    started = time.time()
    outcome, stopped_by = walk_leg(call, x, y, theta)
    after = motion.getRobotPosition(True)

    warnings = []
    delta = odometry_delta(before, after)
    if delta is None:
        warnings.append(NAN_ODOMETRY)
    return {
        "result": outcome,
        "commanded": {"x": rounded(x), "y": rounded(y), "theta": rounded(theta)},
        "clamped": clamped,
        "odometry_before": pose(before),
        "odometry_after": None if is_nan(after) else pose(after),
        "odometry_delta": delta,
        "duration_s": round(time.time() - started, 1),
        "stopped_by": stopped_by,
        "warnings": warnings,
        "state": read_state(call.p),
    }


def angle_diff(a, b):
    return math.atan2(math.sin(a - b), math.cos(a - b))


@primitive("go")
def go(call):
    """Turn to the bearing, then walk straight (moveTo holds the heading on odometry)."""
    motion = call.p["ALMotion"]
    distance = max(0.0, call.args.get("distance_m", GO_MAX_DISTANCE))
    clamped = []
    if distance > GO_MAX_DISTANCE:
        clamped.append("distance %.2f m clamped to %.1f m" % (distance, GO_MAX_DISTANCE))
        distance = GO_MAX_DISTANCE
    bearing = angle_diff(math.radians(call.args["bearing_deg"]), 0.0)
    before = check_can_walk(call)
    started = time.time()
    outcome, stopped_by = "completed", None
    if abs(bearing) > 0.05:
        outcome, stopped_by = walk_leg(call, 0.0, 0.0, bearing)
    if outcome == "completed" and distance > 0.03:
        outcome, stopped_by = walk_leg(call, distance, 0.0, 0.0)
    if outcome == "completed":
        outcome = "arrived"
    after = motion.getRobotPosition(True)
    return {"result": outcome,
            "commanded": {"bearing_deg": rounded(math.degrees(bearing), 1), "distance_m": rounded(distance, 2)},
            "clamped": clamped, "stopped_by": stopped_by,
            "odometry_before": pose(before), "odometry_after": None if is_nan(after) else pose(after),
            "odometry_delta": odometry_delta(before, after), "duration_s": round(time.time() - started, 1),
            "state": read_state(call.p)}


@primitive("say")
def say(call):
    """Speak without waiting for it (it can run during a walk); NAOqi queues overlapping calls."""
    if "ALTextToSpeech" not in call.p:
        raise Refused("this NAOqi has no ALTextToSpeech")
    text = " ".join(call.args["text"].split())[:MAX_SAY_CHARS]
    if not text:
        raise BadArgs("text is empty")
    call.p["ALTextToSpeech"].post.say(text)
    return {"result": "speaking", "text": text}


@primitive("rest")
def rest(call):
    running = movement.current
    ok = stop_and_rest(call.p, "rest requested")
    return {"result": "resting" if ok else "rest_failed", "interrupted": running,
            "state": read_state(call.p)}


# ---------------------------------------------------------------------------- argument checks

def load_registry():
    with open(REGISTRY_PATH) as f:
        entries = json.load(f)["primitives"]
    registry = dict((entry["name"], entry) for entry in entries)
    for entry in entries:
        if entry.get("kind") not in ("read", "action", "move", "override"):
            raise SystemExit("primitives.json: %s has bad kind %r" % (entry["name"], entry.get("kind")))
    missing = set(registry) - set(HANDLERS)
    extra = set(HANDLERS) - set(registry)
    if missing or extra:
        raise SystemExit("primitives.json and bridge.py handlers differ: no handler for %s, "
                         "no registry entry for %s" % (sorted(missing), sorted(extra)))
    return registry


def reject_constant(name):
    raise BadArgs("%s is not allowed" % name)


def validate(schema, args):
    """The subset of JSON Schema primitives.json uses: object, properties, required, types, enum."""
    if not isinstance(args, dict):
        raise BadArgs("arguments must be a JSON object")
    properties = schema.get("properties", {})
    for key in args:
        if key not in properties:
            raise BadArgs("unknown argument '%s' (allowed: %s)" % (key, ", ".join(sorted(properties)) or "none"))
    for key in schema.get("required", []):
        if key not in args:
            raise BadArgs("missing argument %r" % key)
    clean = {}
    for key, value in args.items():
        spec = properties[key]
        kind = spec.get("type")
        if kind in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, long, float)):
                raise BadArgs("%s must be a number" % key)
            if math.isnan(value) or math.isinf(value):
                raise BadArgs("%s must be finite" % key)
            if kind == "integer" and value != int(value):
                raise BadArgs("%s must be an integer" % key)
            value = int(value) if kind == "integer" else float(value)
        elif kind == "string":
            if not isinstance(value, basestring):
                raise BadArgs("%s must be a string" % key)
            value = value.encode("utf-8") if isinstance(value, unicode) else value  # pynaoqi wants str
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise BadArgs("%s must be true or false" % key)
        if "enum" in spec and value not in spec["enum"]:
            raise BadArgs("%s must be one of %s" % (key, spec["enum"]))
        clean[str(key)] = value
    return clean


# ---------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "naoqi-bridge/1"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def reply(self, status, body):
        self.sent = (status, body)
        data = json.dumps(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        lease.touch()
        if self.path != "/health":
            return self.reply(404, {"ok": False, "error": "GET /health, POST /heartbeat or POST /call/<name>"})
        try:
            p = self.server.robot.get()
            return self.reply(200, {"ok": True, "naoqi": "%s:%d" % (self.server.args.ip, self.server.args.port),
                                    "busy": movement.current, "protections": protections(p),
                                    "obstacle_protection": obstacle_protection(p),
                                    "autonomy": autonomy_status(self.server.robot.ap, p["ALMotion"])})
        except RuntimeError as e:
            self.server.robot.check()
            return self.reply(503, {"ok": False, "error": "NAOqi unreachable: %s" % naoqi_message(e)})

    def do_POST(self):
        lease.touch()
        if self.path == "/heartbeat":
            return self.reply(200, {"ok": True})
        received = time.time()
        run_id = call_log.run()
        name = self.path[len("/call/"):] if self.path.startswith("/call/") else None
        if name == "rest":
            call_log.rest_received(run_id, received)  # first thing, before the robot stops or crouches
        self.sent = self.arguments = None
        try:
            self.call_primitive(name)
        finally:
            if run_id:
                self.log_call(run_id, name, received)

    def log_call(self, run_id, name, received):
        status, body = self.sent or (None, None)
        record = {"type": "call", "run_id": run_id, "tool": name, "arguments": self.arguments,
                  "received": received, "returned": time.time()}
        if status == 200:
            record["outcome"] = "ok"
            record["result"] = CallLog.without_pixels(body["result"])
        else:
            record["outcome"] = CallLog.OUTCOMES.get(status, "bridge_error")  # no reply: the handler crashed
            record["error"] = body.get("error") if body else None
        call_log.write(run_id, record)

    def call_primitive(self, name):
        entry = self.server.registry.get(name)
        if entry is None:
            return self.reply(404, {"ok": False, "error": "unknown primitive %r (known: %s)"
                                    % (name, ", ".join(sorted(self.server.registry)))})
        p = None
        try:
            length = int(self.headers.getheader("Content-Length") or 0)
            if length > 65536:
                raise BadArgs("request too large")
            raw = self.rfile.read(length) if length else "{}"
            self.arguments = raw  # as received; replaced by the parsed object when it is valid JSON
            try:
                args = json.loads(raw or "{}", parse_constant=reject_constant)
            except ValueError as e:
                raise BadArgs("body is not valid JSON: %s" % e)
            self.arguments = args
            args = validate(entry["params"], args)
            try:
                p = self.server.robot.get()
            except RuntimeError as e:
                return self.reply(503, {"ok": False, "error": "NAOqi unreachable at %s:%d: %s"
                                        % (self.server.args.ip, self.server.args.port, naoqi_message(e))})
            log("call %s %s" % (name, json.dumps(args)))
            call = Call(p, args, self.connection)
            if entry["kind"] == "move":
                movement.begin(name)
                try:
                    result = HANDLERS[name](call)
                finally:
                    movement.end()
                if "state" in result:  # read inside the slot; it's free again now
                    result["state"]["busy"] = movement.current
            else:
                try:
                    result = HANDLERS[name](call)
                except RuntimeError:
                    if entry["kind"] != "read" or not self.server.robot.check():
                        raise
                    call.p = p = self.server.robot.get()  # NAOqi restarted: reads are safe to retry
                    result = HANDLERS[name](call)
            if self.server.args.auto_image and name not in ("look", "look_around", "say"):
                attach_view(call.p, result)  # after the movement ended: the view it ended with
            result["battery"] = read_battery(call.p)
            return self.reply(200, {"ok": True, "result": result})
        except BadArgs as e:
            return self.reply(400, {"ok": False, "error": "bad arguments for %s: %s" % (name, e)})
        except Refused as e:
            log("refused %s: %s" % (name, e))
            return self.reply(409, {"ok": False, "error": "refused: %s" % e, "state": self.try_state(p),
                                    "battery": read_battery(p) if p is not None else None})
        except RuntimeError as e:
            log("NAOqi error in %s: %s" % (name, naoqi_message(e)))
            self.server.robot.check()
            return self.reply(502, {"ok": False, "error": "NAOqi error in %s: %s" % (name, naoqi_message(e)),
                                    "state": self.try_state(p),
                                    "battery": read_battery(p) if p is not None else None})

    @staticmethod
    def try_state(p):
        try:
            return read_state(p) if p is not None else None
        except Exception:
            return None


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def monitor(args, shutting_down):
    """Background checks every MONITOR_PERIOD: falls (-> stop) and the client lease (-> rest);
    every AUTONOMY_CHECK_PERIOD that NAOqi's own behaviours stayed off (-> rest, off again)."""
    robot = Robot(args)
    last_stamp = last_p = None
    next_autonomy_check = 0.0
    while not shutting_down.is_set():
        try:
            p = robot.get()
            if p is not last_p:  # (re)connected: a restarted NAOqi has its own robotHasFallen timestamp
                last_stamp, last_p = None, p
            memory = p["ALMemory"]
            try:
                stamp = tuple(memory.getTimestamp("robotHasFallen")[1:])  # changes every time it's raised
            except RuntimeError:  # the key may only exist after the first fall
                stamp = None
            if last_stamp is not None and stamp != last_stamp:
                fall = "robotHasFallen raised"
            elif memory.getData("ALMotion/RobotIsFalling"):
                fall = "ALMotion/RobotIsFalling"
            else:
                family = p["ALRobotPosture"].getPostureFamily()
                fall = "posture family %s" % family if family in FALLEN_FAMILIES else None
            last_stamp = stamp
            if fall and not guard.fallen:
                guard.note(fall)
                run_id = call_log.run()
                call_log.write(run_id, {"type": "fall", "run_id": run_id, "time": time.time(), "cause": fall})
                movement.interrupt(p, "fell")
            if lease.expired():
                lease.fired = True
                if movement.current or p["ALMotion"].robotIsWakeUp():
                    log("no request from the client for %.0f s" % lease.timeout)
                    stop_and_rest(p, "client lost")
            if time.time() >= next_autonomy_check:
                next_autonomy_check = time.time() + AUTONOMY_CHECK_PERIOD
                status = autonomy_status(robot.ap, p["ALMotion"])
                back = autonomy_back(status)
                if back:  # e.g. the chest button was double-pressed
                    log("autonomy re-enabled (%s), disabling" % ", ".join(back))
                    stop_and_rest(p, "autonomy re-enabled")
                    log("autonomy off: %s" % json.dumps(quiet_autonomy(robot.ap, p["ALMotion"])))
                elif status["breath"] or status["idle_posture"]:
                    log("breathing / idle posture back on, disabling")
                    quiet_motion(p["ALMotion"])
                if not p["ALMotion"].getExternalCollisionProtectionEnabled("Move"):
                    log("external collision protection for Move is off, turning it on")
                    collision_protection_on(p["ALMotion"])
        except RuntimeError as e:
            log("monitor: NAOqi error: %s" % naoqi_message(e))
            robot.check()
            time.sleep(1.0)
        time.sleep(MONITOR_PERIOD)


call_log = CallLog(None)


def main():
    global lease, camera, move_config, call_log

    def extra(parser):
        parser.add_argument("--listen", default="0.0.0.0")
        parser.add_argument("--listen-port", type=int, default=int(os.environ.get("BRIDGE_PORT", 8765)))
        parser.add_argument("--client-timeout", type=float,
                            default=float(os.environ.get("BRIDGE_CLIENT_TIMEOUT", 20)),
                            help="rest if awake and no request for this many s (0 = off)")
        parser.add_argument("--auto-image", type=int, choices=(0, 1),
                            default=int(os.environ.get("BRIDGE_AUTO_IMAGE", 1)),
                            help="attach top + bottom camera images to every successful result (1 = on)")
        parser.add_argument("--image-grid", type=int, choices=(0, 1),
                            default=int(os.environ.get("BRIDGE_IMAGE_GRID", 1)),
                            help="draw ground-distance lines on camera images (1 = on)")
        parser.add_argument("--max-step-x", type=float,
                            default=float(os.environ.get("BRIDGE_MAX_STEP_X", 0.06)),
                            help="gait step length (m); NAOqi's default is 0.04, its maximum 0.08")

    args = parse_args(__doc__, extra)
    call_log = CallLog(os.environ.get("BRIDGE_RUN_DIR") or None)
    lease = Lease(args.client_timeout)
    camera = Camera(args)
    move_config = [["MaxStepX", min(0.08, max(0.02, args.max_step_x))]]
    server = Server((args.listen, args.listen_port), Handler)
    server.args = args
    server.registry = load_registry()
    server.robot = Robot(args, sonar=True, quiet=True)
    try:
        p = server.robot.get()
        log("connected to NAOqi at %s:%d, protections %s" % (args.ip, args.port, json.dumps(protections(p))))
        off = sorted(name for name, on in protections(p).items() if not on)
        if off:
            log("protections off on this NAOqi: %s" % ", ".join(off))
    except RuntimeError as e:
        log("WARNING: NAOqi not reachable yet at %s:%d (%s); will retry per request"
            % (args.ip, args.port, naoqi_message(e)))

    shutting_down = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutting_down.set())
    signal.signal(signal.SIGINT, lambda *_: shutting_down.set())
    for target, targs in ((server.serve_forever, ()), (monitor, (args, shutting_down))):
        thread = threading.Thread(target=target, args=targs)
        thread.daemon = True
        thread.start()
    log("bridge listening on %s:%d, primitives: %s, client timeout %.0f s, auto image %s, "
        "grid %s, move config %s"
        % (args.listen, args.listen_port, ", ".join(sorted(server.registry)), args.client_timeout,
           "on" if args.auto_image else "off", "on" if args.image_grid else "off",
           move_config))
    while not shutting_down.is_set():
        time.sleep(0.2)

    log("shutting down")
    server.shutdown()
    try:
        p = server.robot.get()
        stop_and_rest(p, "bridge shutdown")
        proxies(args, "ALSonar")["ALSonar"].unsubscribe(SONAR_NAME)
        camera.close()
    except RuntimeError as e:
        log("WARNING: could not rest the robot on shutdown: %s" % naoqi_message(e))
    log("bye")


if __name__ == "__main__":
    main()
