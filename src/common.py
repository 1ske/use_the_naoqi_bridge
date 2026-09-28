"""Shared NAOqi helpers (Python 2.7, pynaoqi 2.1.4).

Safety rules (README "Safety") live here so all NAOqi code follows them:
- speeds are clamped to MAX_JOINT_SPEED / MAX_POSTURE_SPEED
- safe_session() always leaves the robot limp and resting, also on errors and Ctrl-C
- nothing here touches the fall manager or foot-contact protection
"""
from __future__ import print_function

import argparse
import os
import sys
from contextlib import contextmanager

from naoqi import ALProxy

MAX_JOINT_SPEED = 0.3    # fractionMaxSpeed for setAngles
MAX_POSTURE_SPEED = 0.5  # speed for goToPosture


def parse_args(description, extra=None):
    """--ip / --port for every script; `extra(parser)` adds script-specific args."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--ip", default=os.environ.get("NAO_IP", "127.0.0.1"),
                        help="robot or naoqisim address (default: $NAO_IP or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9559)
    if extra:
        extra(parser)
    return parser.parse_args()


def proxies(args, *names):
    """{name: ALProxy} for the given NAOqi modules."""
    return dict((name, ALProxy(name, args.ip, args.port)) for name in names)


def clamp(value, maximum, what="speed"):
    if value > maximum:
        print("%s %.2f clamped to %.2f" % (what, value, maximum))
        return maximum
    return value


def end_limp(motion):
    """Stop walking, then rest() if awake, else Body stiffness 0. Never raises; returns success."""
    try:
        motion.stopMove()
        if motion.robotIsWakeUp():
            print("rest()")
            motion.rest()
        else:
            motion.setStiffnesses("Body", 0.0)
        return True
    except Exception as e:  # e.g. connection lost; nothing more we can do from here
        print("WARNING: could not put the robot in rest: %s" % e, file=sys.stderr)
        return False


@contextmanager
def safe_session(motion, wake=True):
    """Optionally wakeUp(); on exit always end limp (end_limp), also on errors and Ctrl-C."""
    try:
        if wake:
            motion.wakeUp()
        yield
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    finally:
        end_limp(motion)
