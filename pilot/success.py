"""Success check for the simulated tasks, computed after the run from the supervisor's saved values.

    python3 pilot/success.py WORLD GROUND_TRUTH.json [--d 1.0]   -> JSON verdict
    python3 pilot/success.py --plot OUT.png [--d 1.0]             -> both worlds' floor plans with their areas

Success: the run ended with rest (the supervisor saved the pose at the moment the bridge received it), the
robot was upright then, and the torso's floor projection lies in the success area: the part of the last leg
within d of the goal surface (pilot/config/worlds.json; d is a parameter, default 1.0 m).
"""
import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = json.loads((REPO / "pilot" / "config" / "worlds.json").read_text())


def boxes(world_file):
    """Axis-aligned Box solids of a world: DEF name -> (xmin, xmax, ymin, ymax)."""
    text = (REPO / world_file).read_text()
    found = {}
    for block in re.split(r"\n(?=DEF )", text)[1:]:
        name = block.split()[1]
        size = re.search(r"Box\s*\{\s*size\s+([-\d.e]+)\s+([-\d.e]+)\s+([-\d.e]+)", block)
        translation = re.search(r"^\s*translation\s+([-\d.e]+)\s+([-\d.e]+)\s+([-\d.e]+)", block, re.M)
        if not (size and translation) or block.split()[2] != "Solid":
            continue
        rotation = re.search(r"^\s*rotation\s+([-\d.e]+)\s+([-\d.e]+)\s+([-\d.e]+)\s+([-\d.e]+)", block, re.M)
        if rotation and float(rotation.group(4)) != 0.0:
            raise ValueError("%s: %s is rotated; only axis-aligned boxes are supported" % (world_file, name))
        (x, y, _), (sx, sy, _) = ([float(v) for v in m.groups()] for m in (translation, size))
        found[name] = (x - sx / 2, x + sx / 2, y - sy / 2, y + sy / 2)
    return found


def area(world, d=None):
    """Success area of a world for distance d: (xmin, xmax, ymin, ymax), plus the goal surface's y."""
    d = CONFIG["d_default"] if d is None else d
    spec = CONFIG["worlds"][world]
    b = boxes(spec["file"])
    leg = spec["last_leg"]
    assert spec["goal"]["surface"] == "south face" and leg["starts_at"].startswith("south end of ")
    xmin = b[leg["west_wall"]][1]               # inner (east) face of the west wall
    xmax = b[leg["east_wall"]][0]               # inner (west) face of the east wall
    goal_y = b[spec["goal"]["solid"]][2]        # south face of the goal solid
    leg_start = b[leg["starts_at"][len("south end of "):]][2]
    return {"x": [round(xmin, 4), round(xmax, 4)], "y": [round(max(leg_start, goal_y - d), 4), round(goal_y, 4)],
            "goal_surface_y": round(goal_y, 4), "last_leg_y": [round(leg_start, 4), round(goal_y, 4)], "d": d}


def distance_to_goal(world, torso_xy):
    """Distance (m) from the torso's floor projection to the goal surface, where a straight line to it is the
    shortest path: in the last leg, or in the corner square just before it. None elsewhere (in an earlier leg the
    shortest path goes round the corners, which is not computed)."""
    a = area(world)
    x, y = torso_xy
    width = a["x"][1] - a["x"][0]
    if a["x"][0] <= x <= a["x"][1] and a["last_leg_y"][0] - width <= y <= a["goal_surface_y"]:
        return round(a["goal_surface_y"] - y, 3)
    return None


def verdict(world, ground_truth, d=None):
    a = area(world, d)
    rest = ground_truth.get("rest")
    if not rest:
        return {"success": False, "reason": "no rest: the run did not end with the stop signal", "area": a}
    x, y = rest["torso"][0], rest["torso"][1]
    inside = a["x"][0] <= x <= a["x"][1] and a["y"][0] <= y <= a["y"][1]
    upright = rest["tilt_deg"] <= CONFIG["upright"]["max_tilt_deg"]
    reason = ("in the success area and upright" if inside and upright else
              "not upright (tilt %.0f deg)" % rest["tilt_deg"] if inside else
              "torso outside the success area")
    return {"success": inside and upright, "reason": reason, "inside_area": inside, "upright": upright,
            "torso_xy": [round(x, 3), round(y, 3)], "area": a}


def plot(out, d=None, points=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    worlds = list(CONFIG["worlds"])
    fig, axes = plt.subplots(1, len(worlds), figsize=(14, 7.5))
    for ax, world in zip(axes, worlds):
        spec = CONFIG["worlds"][world]
        b = boxes(spec["file"])
        a = area(world, d)
        for name, (x0, x1, y0, y1) in b.items():
            if name in ("FLOOR",) or x1 - x0 > 8:
                continue
            goal = name == spec["goal"]["solid"]
            ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, color="#2a9d8f" if goal else "#555555"))
        ax.add_patch(Rectangle((a["x"][0], a["y"][0]), a["x"][1] - a["x"][0], a["y"][1] - a["y"][0],
                               facecolor="#e9c46a", alpha=0.55, edgecolor="#b8860b", linewidth=1.5,
                               label="success area (d = %.1f m)" % a["d"]))
        ax.plot(a["x"], [a["goal_surface_y"]] * 2, color="#d62828", linewidth=3, label="goal surface")
        ax.plot([a["x"][0], a["x"][1]], [a["last_leg_y"][0]] * 2, color="#264653", linestyle="--",
                linewidth=1, label="start of the last leg")
        start = re.search(r"DEF NAO Nao \{\s*translation\s+([-\d.]+)\s+([-\d.]+)",
                          (REPO / spec["file"]).read_text())
        ax.plot(float(start.group(1)), float(start.group(2)), marker=">", markersize=11, color="#1d3557",
                linestyle="none", label="start pose")
        for p in (points or {}).get(world, []):
            ax.plot(p["xy"][0], p["xy"][1], marker="o" if p["success"] else "X", markersize=9,
                    color="#2b9348" if p["success"] else "#d00000", linestyle="none")
            ax.annotate(p["label"], p["xy"], textcoords="offset points", xytext=(7, 5), fontsize=8)
        ax.set_title("%s (%s)\narea x %.2f..%.2f, y %.2f..%.2f m" % (spec["task"], Path(spec["file"]).name,
                     a["x"][0], a["x"][1], a["y"][0], a["y"][1]))
        ax.set_aspect("equal")
        ax.autoscale_view()
        ax.grid(alpha=0.3)
        ax.set_xlabel("x (m)")
        ax.set_ylabel("y (m)")
        ax.legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=130)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("world", nargs="?")
    parser.add_argument("ground_truth", nargs="?")
    parser.add_argument("--d", type=float)
    parser.add_argument("--plot")
    args = parser.parse_args()
    if args.plot:
        plot(args.plot, args.d)
    else:
        print(json.dumps(verdict(args.world, json.loads(Path(args.ground_truth).read_text()), args.d), indent=2))
