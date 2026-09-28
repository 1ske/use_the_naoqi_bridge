#!/usr/bin/env python
"""HTTP/JSON bridge from the MCP server to NAOqi 2.1.4: safe movement primitives only.

    POST /call/<primitive>  body: JSON object of arguments
        200 {"ok": true, "result": {...}}
        4xx/5xx {"ok": false, "error": "...", "state": {...}}  (state when it could be read)
    POST /heartbeat         keeps the client lease alive (the MCP server sends it every few s)
    GET  /health            NAOqi connection, protections, current movement

The primitives, their arguments and their kind (read / move / override) are listed in
primitives.json. Each one has exactly one handler below (@primitive). The bridge owns safety
and trusts none of the arguments it receives:
- arguments are validated against primitives.json, walks are clamped (1.0 m, pi rad per call),
  posture speed <= MAX_POSTURE_SPEED
- walks stop on their own when a foot bumper is pressed, and forward walks also when the sonar sees
  something closer than --obstacle-stop m (and don't start then); NAOqi's own collision stop is
  off in naoqisim
- camera images: every successful result carries one JPEG with the top view above the bottom view
  ("images", --auto-image) and ground-distance lines (--image-grid); `look` grabs it without moving,
  `look_around` and `scan` turn the head (clamped, joint speed <= MAX_JOINT_SPEED) and return one
  panorama; a missing camera only warns
- `scan`, `go` and `approach` measure free floor from the cameras (perception.py): `go` walks up to
  GO_MAX_DISTANCE m in one call and stops by itself STOP_MARGIN m before anything in its lane, on
  top of the sonar and bumper stops every walk has
- one movement at a time; stop and rest always run and interrupt it
- walking needs an awake, standing robot, finite odometry and no unresolved fall
- a fall stops the current movement and blocks walking until stand succeeds
- losing the client (no request for --client-timeout s, or the caller hangs up during a
  movement) and shutting down (SIGTERM/SIGINT) stop movement and put the robot in rest
- the fall manager, foot-contact and collision protections are never touched
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

import perception
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
STOP_MARGIN = 0.3                   # m; go stops this far before an obstacle in its lane
LANE_HALF_WIDTH = 0.2               # m; the robot is ~0.3 m wide at the shoulders
GO_PERIOD = 0.2                     # s between go's camera checks
MAX_TURN_SPEED = 0.5                # normalized moveToward theta while go holds its heading
APPROACH_TRIES = 3
BODY_FREE_YAW = 0.3                 # rad; beyond this head yaw the bottom camera sees the robot's arms
BOTTOM_EDGE = 0.1                   # of the width; the bottom camera's corners see the hands
FLOOR_SONAR_MIN = 0.5               # m; learn a new floor colour only with no sonar echo closer
SONAR_CONE = 1.05                   # rad; a sonar can echo off things up to ~60 deg to its side
SIDE_PITCH = 0.5                    # rad; scan looks down to the sides (NAOqi allows ~0.2-0.35 there)
MAP_TRAVEL = 0.6                    # m of walking (odometry) an obstacle point is remembered for
MAP_MAX_POINTS = 4000
MAP_RADIUS = 1.5                    # m around the robot that go takes remembered points from

D = "Device/SubDeviceList/"
BATTERY_KEY = D + "Battery/Charge/Sensor/Value"  # 0..1; ALBattery.getBatteryCharge() is 0 in naoqisim
SONAR_KEYS = [D + "US/Left/Sensor/Value", D + "US/Right/Sensor/Value"]
TILT_KEYS = [D + "InertialSensor/AngleX/Sensor/Value", D + "InertialSensor/AngleY/Sensor/Value"]
BUMPER_KEYS = [D + "LFoot/Bumper/Left/Sensor/Value", D + "LFoot/Bumper/Right/Sensor/Value",
               D + "RFoot/Bumper/Left/Sensor/Value", D + "RFoot/Bumper/Right/Sensor/Value"]
SONAR_NOTE = ("2.55 = no echo, NOT proof of free space: the two cones point ~30 deg left/right and "
              "miss surfaces that aren't square to them. 0.25 = 0.25 m or closer.")
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

    def __init__(self, args, sonar=False):
        self.args = args
        self.sonar = sonar
        self.p = None

    def get(self):
        if self.p is None:
            p = proxies(self.args, "ALMotion", "ALRobotPosture", "ALMemory")
            if self.sonar:  # sonar values only update while someone is subscribed
                try:
                    proxies(self.args, "ALSonar")["ALSonar"].subscribe(SONAR_NAME)
                except RuntimeError as e:
                    log("WARNING: no sonar: %s" % naoqi_message(e))
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
    """The single movement slot. stop/rest set stop_requested; the running move sees it and ends."""

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
            raise Refused("busy: %s is still running (one movement at a time). Call stop or rest "
                          "to interrupt it, or wait for it to finish." % self.current)
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

    def wait_idle(self, timeout):
        deadline = time.time() + timeout
        while self.current is not None and time.time() < deadline:
            time.sleep(0.05)


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


def draw_grid(image, distances, pose):
    """Draw a line where the ground is d m ahead of the camera, for each d. Returns the distances drawn."""
    width, rows = image.size
    draw = ImageDraw.Draw(image)
    drawn = []
    for distance in distances:
        row = perception.grid_row(distance, image.size, pose)
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
obstacle_stop_m = 0.4  # replaced in main() with --obstacle-stop
move_config = []  # replaced in main() with --max-step-x


def read_state(p):
    motion, posture, memory = p["ALMotion"], p["ALRobotPosture"], p["ALMemory"]
    odometry = motion.getRobotPosition(True)
    family = posture.getPostureFamily()
    values = memory.getListData([BATTERY_KEY] + SONAR_KEYS + BUMPER_KEYS)
    battery, sonar_left, sonar_right = values[:3]
    bumpers = [bool(v and v > 0.5) for v in values[3:]]
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
        "battery_percent": None if battery is None else rounded(battery * 100, 0),
        "sonar_m": {"left": rounded(sonar_left, 2), "right": rounded(sonar_right, 2), "note": SONAR_NOTE},
        "bumpers": {"left_foot": bumpers[0] or bumpers[1], "right_foot": bumpers[2] or bumpers[3]},
        "busy": movement.current,
        "fall_count": guard.count,
        "last_fall": guard.last,
        "warnings": warnings,
    }


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


def obstacle_ahead(memory):
    """The closest sonar distance if it's below the stop distance, else None."""
    seen = [v for v in memory.getListData(SONAR_KEYS) if v is not None and not math.isnan(v)]
    near = min(seen) if seen else None
    return near if near is not None and near < obstacle_stop_m else None


def bumper_pressed(memory):
    return any(v and v > 0.5 for v in memory.getListData(BUMPER_KEYS))


def protections(p):
    motion = p["ALMotion"]
    return {
        "fall_manager": motion.getFallManagerEnabled(),
        "external_collision_move": motion.getExternalCollisionProtectionEnabled("Move"),
        "external_collision_arms": motion.getExternalCollisionProtectionEnabled("Arms"),
        "self_collision_arms": motion.getCollisionProtectionEnabled("Arms"),
    }


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


def sweep(call, yaws, pitch=0.0, side_pitch=None):
    """Turn the head to each yaw and grab top + bottom Frames there; face forward again after.
    side_pitch: the pitch to use instead where |yaw| > BODY_FREE_YAW.
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
            tilt = side_pitch if side_pitch is not None and abs(yaw) > BODY_FREE_YAW else pitch
            if not turn_head(call, max(-HEAD_YAW_MAX, min(HEAD_YAW_MAX, yaw)), max(HEAD_PITCH_MIN, min(HEAD_PITCH_MAX, tilt))):
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


class Seen(object):
    """What a set of Frames shows: free-floor samples (camera, x, y, kind) in FRAME_ROBOT and the
    floor colour they were measured against (None: free space unknown).

    Bottom-camera frames only count while the head looks ahead: turned sideways that camera sees
    the robot's own hands and shoulders. The floor colour comes from the ground at the feet (bottom
    camera, ahead). Right in front of a plain wall that patch is wall and everything would look
    free, so a new colour is only taken when it matches the floor already known (Seen.known), or
    it hardly shows above the horizon in the top camera (floor can't be there, a wall ahead is), or
    the sonar has nothing within FLOOR_SONAR_MIN; otherwise the known floor is used."""
    known = None

    def __init__(self, p, frames):
        self.frames = [frame for frame in frames if frame and frame.upright
                       and (frame.camera == "top" or abs(frame.head[0]) < BODY_FREE_YAW)]
        bottoms = [frame for frame in self.frames if frame.camera == "bottom"]
        model = perception.floor_model(bottoms[0].image) if bottoms else None
        if model is not None and not (Seen.known is not None and same_floor(model, Seen.known)):
            tops = [frame for frame in self.frames if frame.camera == "top" and abs(frame.head[0]) < BODY_FREE_YAW]
            sky = perception.above_horizon(tops[0].image, tops[0].pose, model) if tops else None
            sonar = [v for v in p["ALMemory"].getListData(SONAR_KEYS) if v is not None and not math.isnan(v)]
            if not ((sky is not None and sky < 0.3) or (sonar and min(sonar) >= FLOOR_SONAR_MIN)):
                model = None
        if model is not None:
            Seen.known = model
        self.floor = Seen.known
        self.samples = [] if self.floor is None else [
            (frame.camera,) + sample for frame in self.frames
            for sample in perception.free_space(frame.image, frame.pose, self.floor,
                                                edge=BOTTOM_EDGE if frame.camera == "bottom" else 0.0)]

    def colors(self, names=None):
        """[{"color", "bearing_deg", "distance_m", ...}] for every colour seen, most pixels first."""
        found = []
        for name in names or sorted(perception.COLORS):
            hits = [hit for hit in (perception.find_color(frame.image, frame.pose, name) for frame in self.frames)
                    if hit]
            if hits:  # direction from the biggest view of it, distance from the nearest estimate
                best = dict(max(hits, key=lambda hit: hit["pixels"]), color=name)
                best.pop("upper_bound", None)
                measured = [hit for hit in hits if hit["distance_m"] is not None]
                if measured:
                    nearest = min(measured, key=lambda hit: hit["distance_m"])
                    best["distance_m"] = nearest["distance_m"]
                    if nearest.get("upper_bound"):
                        best["upper_bound"] = True
                found.append(best)
        return sorted(found, key=lambda hit: -hit["pixels"])


class LocalMap(object):
    """Obstacle points seen recently, kept in odometry coordinates, so a sonar echo off a wall that
    has left the cameras' view (beside the robot) can still be explained. Odometry drifts (the
    simulator walks ~70-86% of what it reports), so points are dropped once the robot has moved
    MAP_TRAVEL m (turns count 0.5 m per rad) since they were seen, and they never steer or stop
    the robot themselves."""

    def __init__(self):
        self.points = []  # (travel when seen, x, y, kind) in the odometry frame
        self.travel = 0.0
        self.last = None

    def moved(self, odom):
        if self.last is not None:
            self.travel += math.hypot(odom[0] - self.last[0], odom[1] - self.last[1]) \
                + 0.5 * abs(angle_diff(odom[2], self.last[2]))
        self.last = odom

    def add(self, samples, odom):
        self.moved(odom)
        c, s = math.cos(odom[2]), math.sin(odom[2])
        for camera, x, y, kind in samples:
            if kind != "open":  # "near" from the top camera: something at most this far
                exact = kind == "obstacle" or camera == "bottom"
                self.points.append((self.travel, odom[0] + c * x - s * y, odom[1] + s * x + c * y,
                                    "obstacle" if exact else "near"))
        self.points = [point for point in self.points if self.travel - point[0] <= MAP_TRAVEL][-MAP_MAX_POINTS:]

    def around(self, odom, radius=MAP_RADIUS):
        """The remembered points within radius m, as ("memory", x, y, kind) in the robot frame."""
        self.moved(odom)
        c, s = math.cos(odom[2]), math.sin(odom[2])
        near = []
        for travel, px, py, kind in self.points:
            dx, dy = px - odom[0], py - odom[1]
            x, y = c * dx + s * dy, -s * dx + c * dy
            if self.travel - travel <= MAP_TRAVEL and math.hypot(x, y) <= radius:
                near.append(("memory", x, y, kind))
        return near


local_map = LocalMap()


def same_floor(a, b):
    return max(abs(x - y) for x, y in zip(a[0], b[0])) <= max(a[1], b[1])


def openings(table, min_free=1.0):
    """Runs of neighbouring clearance bins with at least min_free m: where the robot could go."""
    runs, run = [], []
    for entry in table + [None]:
        if entry is not None and entry["free_m"] >= min_free and not entry.get("upper_bound"):
            run.append(entry)
            continue
        if run:
            best = max(run, key=lambda e: e["free_m"])
            runs.append({"from_deg": run[0]["bearing_deg"], "to_deg": run[-1]["bearing_deg"],
                         "best_bearing_deg": best["bearing_deg"], "free_m": best["free_m"],
                         "blocked": best["blocked"]})
        run = []
    return sorted(runs, key=lambda r: -r["free_m"])


def survey(call, yaws=SWEEP_YAWS):
    """Sweep the head and measure: (outcome, result dict with clearance, openings, colors, panorama)."""
    outcome, columns, missing = sweep(call, yaws, side_pitch=SIDE_PITCH)
    seen = Seen(call.p, [frame for frames in columns for frame in frames])
    odom = call.p["ALMotion"].getRobotPosition(True)
    if not is_nan(odom):
        local_map.add(seen.samples, odom)
    result = {"result": outcome, "images": panorama(columns)}
    if seen.floor is None:
        result["warnings"] = ["floor not recognised (right at an obstacle, or a patterned floor): "
                              "free space unknown, use the image"]
    else:
        table = perception.clearance(seen.samples)
        result.update(clearance=table, openings=openings(table))
    result["colors"] = seen.colors()
    if missing and outcome == "completed":
        result.setdefault("warnings", []).append(NO_IMAGE)
    return outcome, result


@primitive("scan")
def scan(call):
    _, result = survey(call)
    result["state"] = read_state(call.p)
    return result


@primitive("stand")
def stand(call):
    was_fallen = guard.fallen
    call.p["ALMotion"].wakeUp()  # on the H25 this only stiffens; after a fall it restores stiffness
    outcome = go_to_posture(call, "StandInit")
    state = read_state(call.p)
    if outcome == "completed" and state["posture_family"] == "Standing":
        guard.clear()
        state = read_state(call.p)
    elif outcome == "completed":
        outcome = "failed"
    result = {"result": outcome, "state": state}
    if outcome != "completed" and was_fallen:
        result["hint"] = GET_UP_HINT
    return result


@primitive("sit")
def sit(call):
    if guard.fallen:
        raise Refused("the robot fell: call stand first (or rest)")
    outcome = go_to_posture(call, "Sit")
    state = read_state(call.p)
    if outcome == "completed" and state["posture_family"] != "Sitting":
        outcome = "failed"
    return {"result": outcome, "state": state}


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
    before = motion.getRobotPosition(True)
    if is_nan(before):
        raise Refused(NAN_ODOMETRY)
    return before


def walk_guard(memory, forward, stopped_by):
    """wait_task check for walks: bumper, and sonar for forward walks."""
    def check():
        if bumper_pressed(memory):
            stopped_by["bumper"] = True
            return "bumped"
        near = obstacle_ahead(memory) if forward else None
        if near is not None:
            stopped_by["sonar_m"] = rounded(near, 2)
            return "obstacle"
        return None
    return check


def walk_leg(call, x, y, theta):
    """One moveTo with the walk guards. Returns (outcome, stopped_by or None)."""
    motion, memory = call.p["ALMotion"], call.p["ALMemory"]
    forward = x > 0.02
    near = obstacle_ahead(memory) if forward else None
    if near is not None:
        raise Refused("obstacle %.2f m ahead (sonar; forward walks stop below %.2f m): turn, sidestep "
                      "(walk_to with y) or back up" % (near, obstacle_stop_m))
    stopped_by = {}
    motion.moveInit()
    task = motion.post.moveTo(x, y, theta, move_config)
    timeout = 20.0 + 20.0 * math.hypot(x, y) + 10.0 * abs(theta)
    outcome = wait_task(call, motion, task, timeout, check=walk_guard(memory, forward, stopped_by))
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
    else:
        commanded_big = math.hypot(x, y) > 0.05 or abs(theta) > 0.1
        moved = math.hypot(delta["x"], delta["y"]) > 0.02 or abs(delta["theta"]) > 0.05
        if outcome == "completed" and commanded_big and not moved:
            outcome = "did_not_move"
            warnings.append("moveTo finished but odometry did not change: the walk probably did "
                            "nothing (naoqisim odometry/emulation problem, NOTES.md). Restart naoqisim.")
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


def unexplained_echo(memory, samples):
    """The closest sonar echo under the stop distance that the cameras don't explain (nothing seen
    at about that distance on that side), else None. Explained echoes are left to the camera lane
    check, which stops closer in; unexplained ones stop go: the sonar catches what the cameras miss
    (floor-coloured or thin things)."""
    closest = None
    for value, side in zip(memory.getListData(SONAR_KEYS), (1, -1)):
        if value is None or math.isnan(value) or value >= obstacle_stop_m:
            continue
        explained = any(kind != "open" and 0 <= side * y + 0.05 and abs(math.atan2(y, x)) <= SONAR_CONE
                        and (abs(math.hypot(x, y) - value) <= 0.2 if kind == "obstacle"
                             else value - 0.05 <= math.hypot(x, y) <= value + 0.5)  # "near": at most that far
                        for _, x, y, kind in samples)
        if not explained and (closest is None or value < closest):
            closest = value
    return closest


def walk_ahead(call, distance, heading):
    """Walk forward up to `distance` m holding odometry heading `heading`, with moveToward, checking
    the cameras every GO_PERIOD s. Stops STOP_MARGIN m before anything in the robot's lane, and on
    bumper / sonar echo the cameras can't explain / stop / rest / fall / lost client / timeout.
    Returns (outcome, details)."""
    motion, memory = call.p["ALMotion"], call.p["ALMemory"]
    start = motion.getRobotPosition(True)
    deadline = time.time() + 20.0 + 30.0 * distance
    falls = guard.count
    details, outcome = {}, None
    if Seen.known is None:  # learn the floor first: a single frame can't tell floor from a plain wall
        survey(call)
    motion.moveInit()
    try:
        while outcome is None:
            if movement.stop_requested.is_set():
                outcome = movement.reason or "stopped"
            elif guard.count != falls:
                outcome = "fell"
            elif call.client_gone():
                log("client hung up during %s" % movement.current)
                movement.interrupt(call.p, "client_lost")
                outcome = "client_lost"
            elif time.time() > deadline:
                outcome = "timeout"
            elif bumper_pressed(memory):
                details["stopped_by"], outcome = {"bumper": True}, "bumped"
            if outcome:
                break
            lease.touch()
            seen = Seen(call.p, [camera.frame(call.p, name) for name in ("top", "bottom")])
            now = motion.getRobotPosition(True)
            if not seen.samples or is_nan(now):
                outcome = "blind"  # no floor to measure against, a camera missing, or not upright
                details["hint"] = "can't measure the floor ahead: call scan, or walk_to a short step"
                break
            local_map.add(seen.samples, now)
            samples = seen.samples
            echo = unexplained_echo(memory, samples + local_map.around(now))
            if echo is not None:
                details["stopped_by"], outcome = {"sonar_m": rounded(echo, 2)}, "obstacle"
                break
            remaining = distance - math.hypot(now[0] - start[0], now[1] - start[1])
            want = angle_diff(heading, now[2])
            phi, room, straight = perception.best_heading(samples, LANE_HALF_WIDTH, want, STOP_MARGIN, remaining)
            details["free_ahead_m"] = None if straight is None else rounded(straight + STOP_MARGIN, 2)
            if remaining <= 0.03:
                outcome = "arrived"
            elif room is None or room <= 0.03:
                outcome = "blocked"
            else:
                left, right = perception.side_gaps(samples, LANE_HALF_WIDTH)
                sideways = 0.0 if left is None or right is None else max(-0.5, min(0.5, left - right))
                turn = max(-MAX_TURN_SPEED, min(MAX_TURN_SPEED, 2.0 * phi))
                # walk while the lane straight ahead is clear, slower near the end; else turn in place
                forward = 0.0 if straight is None or straight <= 0.03 else min(1.0, max(0.3, min(straight, remaining) / 0.3))
                motion.moveToward(forward if abs(phi) < 0.35 else 0.3 * forward, sideways if forward else 0.0,
                                  turn, move_config)
                time.sleep(GO_PERIOD)
    finally:
        if outcome != "rested":
            motion.stopMove()
            motion.waitUntilMoveIsFinished()
    if outcome in ("timeout", "client_lost"):
        movement.interrupt(call.p, outcome)
    if outcome == "client_lost":
        end_limp(call.p["ALMotion"])
    if guard.count != falls:
        outcome = "fell"
    return outcome, details


def go_to(call, bearing, distance):
    """Turn to `bearing` rad, then walk_ahead `distance` m. Returns the go result without state."""
    motion = call.p["ALMotion"]
    before = check_can_walk(call)
    started = time.time()
    outcome, details = "completed", {}
    if abs(bearing) > 0.05:
        outcome, stopped_by = walk_leg(call, 0.0, 0.0, bearing)
        details["stopped_by"] = stopped_by
    if outcome == "completed" and distance > 0.03:
        heading = motion.getRobotPosition(True)[2]
        outcome, details = walk_ahead(call, distance, heading)
    elif outcome == "completed":
        outcome = "arrived"
    after = motion.getRobotPosition(True)
    result = {"result": outcome, "commanded": {"bearing_deg": rounded(math.degrees(bearing), 1),
                                                "distance_m": rounded(distance, 2)},
              "odometry_before": pose(before), "odometry_after": None if is_nan(after) else pose(after),
              "odometry_delta": odometry_delta(before, after), "duration_s": round(time.time() - started, 1)}
    result.update(details)
    return result


@primitive("go")
def go(call):
    distance = max(0.0, call.args.get("distance_m", GO_MAX_DISTANCE))
    clamped = []
    if distance > GO_MAX_DISTANCE:
        clamped.append("distance %.2f m clamped to %.1f m" % (distance, GO_MAX_DISTANCE))
        distance = GO_MAX_DISTANCE
    bearing = math.radians(call.args["bearing_deg"])
    if abs(bearing) > MAX_WALK_TURN:
        bearing = angle_diff(bearing, 0.0)
    result = go_to(call, bearing, distance)
    result["clamped"] = clamped
    result["state"] = read_state(call.p)
    return result


@primitive("approach")
def approach(call):
    """Find the colour (ahead first, else with a sweep) and go to it, up to APPROACH_TRIES times."""
    color = call.args["color"]
    stop_at = STOP_MARGIN + 0.15
    legs = []
    result = {"result": "not_found", "color": color}
    for _ in range(APPROACH_TRIES):
        hits = Seen(call.p, [camera.frame(call.p, name) for name in ("top", "bottom")]).colors([color])
        if not hits:
            outcome, survey_result = survey(call)
            if outcome != "completed":
                result.update(result=outcome)
                break
            hits = [hit for hit in survey_result["colors"] if hit["color"] == color]
            if not hits:
                survey_result.pop("images", None)
                result.update(result="not_found", survey=survey_result)
                break
        hit = hits[0]
        result["target"] = hit
        if hit["distance_m"] is not None and hit["distance_m"] <= stop_at:
            result["result"] = "arrived"
            break
        distance = GO_MAX_DISTANCE if hit["distance_m"] is None else min(GO_MAX_DISTANCE, hit["distance_m"])
        leg = go_to(call, math.radians(hit["bearing_deg"]), distance)
        legs.append(leg)
        if leg["result"] not in ("arrived", "blocked", "obstacle"):
            result["result"] = leg["result"]
            break
        result["result"] = "blocked"  # until the next look says the colour is what's in the way
    else:
        hits = Seen(call.p, [camera.frame(call.p, name) for name in ("top", "bottom")]).colors([color])
        if hits:
            result["target"] = hits[0]
            if hits[0]["distance_m"] is not None and hits[0]["distance_m"] <= stop_at:
                result["result"] = "arrived"
    result["legs"] = legs
    result["state"] = read_state(call.p)
    return result


@primitive("stop")
def stop(call):
    running = movement.current
    movement.interrupt(call.p, "stopped")
    movement.wait_idle(5.0)
    return {"result": "stopped", "interrupted": running, "state": read_state(call.p)}


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
        if entry.get("kind") not in ("read", "move", "override"):
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
                                    "busy": movement.current, "protections": protections(p)})
        except RuntimeError as e:
            self.server.robot.check()
            return self.reply(503, {"ok": False, "error": "NAOqi unreachable: %s" % naoqi_message(e)})

    def do_POST(self):
        lease.touch()
        if self.path == "/heartbeat":
            return self.reply(200, {"ok": True})
        name = self.path[len("/call/"):] if self.path.startswith("/call/") else None
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
            try:
                args = json.loads(raw or "{}", parse_constant=reject_constant)
            except ValueError as e:
                raise BadArgs("body is not valid JSON: %s" % e)
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
            if self.server.args.auto_image and name not in ("look", "look_around", "scan"):
                attach_view(call.p, result)  # after the movement ended: the view it ended with
            return self.reply(200, {"ok": True, "result": result})
        except BadArgs as e:
            return self.reply(400, {"ok": False, "error": "bad arguments for %s: %s" % (name, e)})
        except Refused as e:
            log("refused %s: %s" % (name, e))
            return self.reply(409, {"ok": False, "error": "refused: %s" % e, "state": self.try_state(p)})
        except RuntimeError as e:
            log("NAOqi error in %s: %s" % (name, naoqi_message(e)))
            self.server.robot.check()
            return self.reply(502, {"ok": False, "error": "NAOqi error in %s: %s" % (name, naoqi_message(e)),
                                    "state": self.try_state(p)})

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
    """Background checks every MONITOR_PERIOD: falls (-> stop) and the client lease (-> rest)."""
    robot = Robot(args)
    last_stamp = last_p = None
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
                movement.interrupt(p, "fell")
            if lease.expired():
                lease.fired = True
                if movement.current or p["ALMotion"].robotIsWakeUp():
                    log("no request from the client for %.0f s" % lease.timeout)
                    stop_and_rest(p, "client lost")
        except RuntimeError as e:
            log("monitor: NAOqi error: %s" % naoqi_message(e))
            robot.check()
            time.sleep(1.0)
        time.sleep(MONITOR_PERIOD)


def main():
    global lease, camera, obstacle_stop_m, move_config

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
        parser.add_argument("--obstacle-stop", type=float,
                            default=float(os.environ.get("BRIDGE_OBSTACLE_STOP_M", 0.4)),
                            help="forward walks stop / don't start when the sonar sees something closer (m)")
        parser.add_argument("--max-step-x", type=float,
                            default=float(os.environ.get("BRIDGE_MAX_STEP_X", 0.06)),
                            help="gait step length (m); NAOqi's default is 0.04, its maximum 0.08")

    args = parse_args(__doc__, extra)
    lease = Lease(args.client_timeout)
    camera = Camera(args)
    obstacle_stop_m = args.obstacle_stop
    move_config = [["MaxStepX", min(0.08, max(0.02, args.max_step_x))]]
    server = Server((args.listen, args.listen_port), Handler)
    server.args = args
    server.registry = load_registry()
    server.robot = Robot(args, sonar=True)
    try:
        p = server.robot.get()
        log("connected to NAOqi at %s:%d, protections %s" % (args.ip, args.port, json.dumps(protections(p))))
        if not all(protections(p).values()):
            log("WARNING: some protections are off on this NAOqi; the bridge never changes them")
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
        "grid %s, obstacle stop %.2f m, move config %s"
        % (args.listen, args.listen_port, ", ".join(sorted(server.registry)), args.client_timeout,
           "on" if args.auto_image else "off", "on" if args.image_grid else "off", obstacle_stop_m,
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
