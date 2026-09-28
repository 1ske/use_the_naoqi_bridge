#!/usr/bin/env python
"""Connect to NAOqi and print its version. Doesn't move the robot."""
from __future__ import print_function

from common import parse_args, proxies

args = parse_args(__doc__)
p = proxies(args, "ALMotion", "ALMemory")
print("connected to %s:%d" % (args.ip, args.port))
print("ALMotion version: %s" % p["ALMotion"].version())
try:
    system = proxies(args, "ALSystem")["ALSystem"]
    print("NAOqi (ALSystem) version: %s" % system.systemVersion())
    print("robot name: %s" % system.robotName())
except RuntimeError as e:
    print("ALSystem not available (%s)" % str(e).splitlines()[0])
for key in ("RobotConfig/Body/Type", "RobotConfig/Body/BaseVersion"):
    try:
        print("%s: %s" % (key, p["ALMemory"].getData(key)))
    except RuntimeError:
        print("%s: n/a" % key)
