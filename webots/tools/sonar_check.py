"""Check the NAO's sonar (and the bridge's obstacle stop) against the simulator's ground truth.

Runs on the Mac with Python 3, next to a running bridge and truth.py logging to a file:
    WEBOTS_HOME=/Applications/Webots.app /Applications/Webots.app/Contents/MacOS/webots-controller \\
        --robot-name=truth webots/tools/truth.py 0.5 > /tmp/truth.txt &
    python3 webots/tools/sonar_check.py webots/worlds/corridor_complex.wbt /tmp/truth.txt

Stand the robot facing a wall first. The script backs away from it in --step m steps (backward
walks aren't sonar-stopped), then turns in place in --turn-deg steps, and after every move prints
both sonar readings next to the true distance to the nearest wall inside each sonar's cone
(NAO V5: axes ~25 deg left / right, ~60 deg wide), from the world file and truth.py's pose.
"""
import argparse
import json
import math
import time
import urllib.request

from world_rays import load_boxes, ray

SONAR_AXIS = math.radians(25)
SONAR_HALF_CONE = math.radians(30)
SONAR_X = 0.05  # m ahead of the torso centre, roughly where the chest transducers are


def call(url, name, args=None):
    request = urllib.request.Request("%s/call/%s" % (url, name), data=json.dumps(args or {}).encode(),
                                     method="POST")
    try:
        body = json.load(urllib.request.urlopen(request, timeout=120))
    except urllib.error.HTTPError as e:
        body = json.load(e)
    if not body.get("ok"):
        raise SystemExit("%s refused: %s" % (name, body.get("error")))
    return body["result"]


def truth_pose(path):
    """(x, y, heading rad) from the last line truth.py wrote."""
    line = open(path).read().strip().splitlines()[-1].split()
    return float(line[1]), float(line[2]), math.radians(float(line[4]))


def cone_distance(boxes, x, y, heading, side):
    """True distance to the nearest wall inside one sonar's cone, and straight along its axis."""
    axis = heading + side * SONAR_AXIS
    sx, sy = x + SONAR_X * math.cos(heading), y + SONAR_X * math.sin(heading)
    steps = 13
    nearest = min(ray(boxes, sx, sy, axis + SONAR_HALF_CONE * (2.0 * i / (steps - 1) - 1))[0]
                  for i in range(steps))
    return nearest, ray(boxes, sx, sy, axis)[0]


def measure(url, boxes, truth, label):
    time.sleep(1.5)  # truth.py prints every 0.5 s; let the robot settle
    state = call(url, "get_state")
    x, y, heading = truth_pose(truth)
    left_true, left_axis = cone_distance(boxes, x, y, heading, 1)
    right_true, right_axis = cone_distance(boxes, x, y, heading, -1)
    ahead = ray(boxes, x + SONAR_X * math.cos(heading), y + SONAR_X * math.sin(heading), heading)[0]
    sonar = state["sonar_m"]
    print("%-14s %6.2f   %5.2f  %5.2f %5.2f   %5.2f  %5.2f %5.2f"
          % (label, ahead, sonar["left"], left_true, left_axis, sonar["right"], right_true, right_axis))
    return sonar, left_true, right_true


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("world")
    parser.add_argument("truth", help="file truth.py is writing to")
    parser.add_argument("--bridge", default="http://127.0.0.1:8765")
    parser.add_argument("--steps", type=int, default=8, help="backward steps")
    parser.add_argument("--step", type=float, default=0.1, help="m per backward step")
    parser.add_argument("--turns", type=int, default=4, help="turn steps after backing off")
    parser.add_argument("--turn-deg", type=float, default=15.0)
    args = parser.parse_args()
    boxes = load_boxes(args.world)

    print("                 true   ------ left ------   ------ right -----")
    print("move            ahead   sonar  cone  axis    sonar  cone  axis   (m; sonar 2.55 = no echo)")
    rows = [measure(args.bridge, boxes, args.truth, "start")]
    for i in range(args.steps):
        call(args.bridge, "walk_to", {"x": -args.step, "y": 0.0, "theta": 0.0})
        rows.append(measure(args.bridge, boxes, args.truth, "back %d" % (i + 1)))
    for i in range(args.turns):
        call(args.bridge, "walk_to", {"x": 0.0, "y": 0.0, "theta": math.radians(args.turn_deg)})
        rows.append(measure(args.bridge, boxes, args.truth, "turn +%d deg" % (args.turn_deg * (i + 1))))

    errors = [abs(sonar[side] - true) for sonar, left, right in rows
              for side, true in (("left", left), ("right", right)) if sonar[side] < 2.5 and true < 2.5]
    misses = sum(1 for sonar, left, right in rows for side, true in (("left", left), ("right", right))
                 if sonar[side] >= 2.5 and true < 1.5)
    if errors:
        print("\necho vs nearest wall in the cone: mean error %.2f m, max %.2f m (%d readings)"
              % (sum(errors) / len(errors), max(errors), len(errors)))
    print("missed walls closer than 1.5 m (sonar said no echo): %d" % misses)


if __name__ == "__main__":
    main()
