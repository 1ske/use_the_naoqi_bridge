"""Ground-truth distances in a world file: how far a horizontal ray runs before it hits a wall.

Runs on the Mac with any Python 3 (no Webots needed). Only understands what our worlds use:
Solids with a translation, an optional rotation about z and a Box geometry (the floor is skipped).

    python3 webots/tools/world_rays.py webots/worlds/corridor_complex.wbt X Y HEADING_DEG
prints the distance to the nearest wall every 10 degrees relative to HEADING_DEG, left to right.
X, Y, heading: from truth.py.
"""
import math
import re
import sys

SOLID = re.compile(r"(?:DEF (\w+) )?Solid \{(.*?)\n\}", re.S)
NUMBERS = r"(-?[\d.]+(?:e-?\d+)?)"


def load_boxes(path):
    """[(name, cx, cy, half_x, half_y, yaw, rgb)] for every box-shaped Solid except the floor."""
    boxes = []
    for name, body in SOLID.findall(open(path).read()):
        size = re.search(r"Box \{ size %s %s %s" % (NUMBERS, NUMBERS, NUMBERS), body)
        where = re.search(r"translation %s %s %s" % (NUMBERS, NUMBERS, NUMBERS), body)
        if not size or not where or (name or "").upper() == "FLOOR":
            continue
        turn = re.search(r"rotation %s %s %s %s" % (NUMBERS, NUMBERS, NUMBERS, NUMBERS), body)
        yaw = float(turn.group(4)) * (1 if float(turn.group(3)) >= 0 else -1) if turn else 0.0
        colour = re.search(r"baseColor %s %s %s" % (NUMBERS, NUMBERS, NUMBERS), body)
        boxes.append((name or "?", float(where.group(1)), float(where.group(2)),
                      float(size.group(1)) / 2, float(size.group(2)) / 2, yaw,
                      tuple(float(c) for c in colour.groups()) if colour else None))
    return boxes


def ray(boxes, x, y, angle, max_range=10.0):
    """(distance, box name) of the first box a ray from (x, y) at world angle `angle` (rad) hits."""
    best = (max_range, None)
    for name, cx, cy, hx, hy, yaw, _ in boxes:
        c, s = math.cos(-yaw), math.sin(-yaw)  # into the box frame
        ox, oy = c * (x - cx) - s * (y - cy), s * (x - cx) + c * (y - cy)
        dx, dy = c * math.cos(angle) - s * math.sin(angle), s * math.cos(angle) + c * math.sin(angle)
        t0, t1 = 0.0, max_range
        for o, d, h in ((ox, dx, hx), (oy, dy, hy)):
            if abs(d) < 1e-9:
                if abs(o) > h:
                    t0, t1 = 1, 0
                continue
            a, b = (-h - o) / d, (h - o) / d
            t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
        if t0 <= t1 and t0 < best[0]:
            best = (t0, name)
    return best


def main():
    world, x, y, heading = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    boxes = load_boxes(world)
    for bearing in range(150, -151, -10):
        distance, name = ray(boxes, x, y, math.radians(heading + bearing))
        print("%5d deg  %5.2f m  %s" % (bearing, distance, name))


if __name__ == "__main__":
    main()
