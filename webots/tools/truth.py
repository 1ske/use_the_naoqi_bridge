"""Print the Webots NAO's real position, heading and torso tilt (ground truth for the scripts).

Runs on the Mac with Webots' own Python (3.x), not in Docker:
    /Applications/Webots.app/Contents/MacOS/webots-controller --robot-name=truth webots/tools/truth.py
Roll/pitch use the same convention as NAOqi's AngleX/AngleY (x forward, y left, z up).
"""
import math
import sys

from controller import Supervisor

PERIOD = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5  # seconds between lines

robot = Supervisor()
step = int(robot.getBasicTimeStep())
nao = robot.getFromDef("NAO")
start = nao.getPosition()
last = -PERIOD
print("  time      x      y   dist  heading   roll  pitch   (m, deg)")
while robot.step(step) != -1:
    t = robot.getTime()
    if t - last < PERIOD:
        continue
    last = t
    x, y, _ = nao.getPosition()
    r = nao.getOrientation()  # row-major 3x3, torso frame -> world
    heading = math.degrees(math.atan2(r[3], r[0]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -r[6]))))
    roll = math.degrees(math.atan2(r[7], r[8]))
    dist = math.hypot(x - start[0], y - start[1])
    print("%6.1f %6.2f %6.2f %6.2f %8.1f %6.1f %6.1f" % (t, x, y, dist, heading, roll, pitch), flush=True)
