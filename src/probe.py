#!/usr/bin/env python
"""Print, as JSON, what this NAOqi exposes: modules and their methods, protection and walk
settings, sonar / navigation / bumper memory keys. Read-only: doesn't move the robot.

    docker compose run --rm app python probe.py --out probe_sim.json
    NAO_IP=<robot ip> docker compose run --rm app python probe.py --out probe_real.json
(src/ is mounted at /app, so the files land in src/; pynaoqi logs to stdout, hence --out.)
"""
from __future__ import print_function

import json
import re

from common import parse_args, proxies

MODULES = ("ALMotion", "ALNavigation", "ALSonar", "ALRobotPosture", "ALVisualCompass", "ALTracker",
           "ALRedBallDetection", "ALLandMarkDetection", "ALMovementDetection", "ALLocalization",
           "ALVisionRecognition", "ALAutonomousLife", "ALBasicAwareness", "ALAutonomousMoves",
           "ALSystem", "ALVideoDevice", "ALTextToSpeech", "ALAnimatedSpeech", "ALAudioDevice", "ALLeds")
MEMORY_KEYS = re.compile(r"Sonar|Navigation|Bumper|US/|Obstacle", re.I)
BASE_METHODS = set(["ping", "version", "getMethodList", "getMethodHelp", "getModuleHelp", "exit",
                    "isRunning", "wait", "stop", "getBrokerName", "getUsage", "innerTest",
                    "isStatsEnabled", "enableStats", "clearStats", "isTraceEnabled", "enableTrace",
                    "pCall", "dataChanged", "setDebugMode"])  # every ALModule has these

args = parse_args(__doc__, lambda parser: parser.add_argument("--out", help="write the JSON here"))


def read(fn, *fn_args):
    """fn(*fn_args), or {"error": ...} if this NAOqi lacks it."""
    try:
        return fn(*fn_args)
    except Exception as e:
        return {"error": str(e).strip().splitlines()[-1] if str(e).strip() else repr(e)}


found = {}
for name in MODULES:
    try:
        found[name] = proxies(args, name)[name]
    except RuntimeError:
        pass

memory = proxies(args, "ALMemory")["ALMemory"]

report = {"address": "%s:%d" % (args.ip, args.port), "modules": {}, "missing": [], "settings": {}}
for name in MODULES:
    if name not in found:
        report["missing"].append(name)
        continue
    methods = read(found[name].getMethodList)
    report["modules"][name] = {
        "version": read(found[name].version),
        "methods": sorted(m for m in methods if m not in BASE_METHODS) if isinstance(methods, list) else methods,
    }

settings = report["settings"]
if "ALSystem" in found:
    settings["system_version"] = read(found["ALSystem"].systemVersion)
    settings["robot_name"] = read(found["ALSystem"].robotName)
for key in ("RobotConfig/Body/Type", "RobotConfig/Body/BaseVersion", "RobotConfig/Body/Version"):
    settings[key] = read(memory.getData, key)
motion = found["ALMotion"]
settings["fall_manager"] = read(motion.getFallManagerEnabled)
for part in ("Move", "Arms", "LArm", "RArm"):
    settings["external_collision_%s" % part] = read(motion.getExternalCollisionProtectionEnabled, part)
settings["self_collision_arms"] = read(motion.getCollisionProtectionEnabled, "Arms")
settings["move_config_default"] = read(motion.getMoveConfig, "Default")
settings["move_config_max"] = read(motion.getMoveConfig, "Max")
settings["foot_contact_protection"] = read(motion.getMotionConfig, [["ENABLE_FOOT_CONTACT_PROTECTION", 0]])
for method in ("getOrthogonalSecurityDistance", "getTangentialSecurityDistance"):
    settings["ALMotion.%s" % method] = read(getattr(motion, method))
if "ALNavigation" in found:
    for method in ("getSecurityDistance",):
        settings["ALNavigation.%s" % method] = read(getattr(found["ALNavigation"], method))
names = read(memory.getDataListName)
report["memory_keys"] = sorted(n for n in names if MEMORY_KEYS.search(n)) if isinstance(names, list) else names

text = json.dumps(report, indent=2, sort_keys=True)
if args.out:
    with open(args.out, "w") as f:
        f.write(text + "\n")
    print("wrote %s" % args.out)
else:
    print(text)
