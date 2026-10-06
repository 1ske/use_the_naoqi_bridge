"""Free self-test of the ground truth and the success check (Webots controller of the `truth` robot):

    PILOT_INSTRUMENTATION_DIR=/tmp/pilot-selftest WEBOTS_HOME=/Applications/Webots.app \
        /Applications/Webots.app/Contents/MacOS/webots-controller --robot-name=truth pilot/supervisor/selftest.py

For each test case it moves the NAO with the supervisor, lets it settle, then plays the bridge's and the
runner's part by creating the run's t0, rest_received and end files. GroundTruth (the same class the pilot
supervisor runs) records the pose and the real-time factor, and pilot/success.py gives the verdict.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import success  # noqa: E402
from pilot_supervisor import GroundTruth  # noqa: E402

from controller import Supervisor  # noqa: E402

EARLIER_LEG = {"corridor": (1.0, 0.0), "corridor_complex": (5.5, 5.3)}  # first leg / leg 5
UPRIGHT_Z, LYING_Z = 0.334, 0.12


def cases(world):
    a = success.area(world)
    cx, cy = sum(a["x"]) / 2, sum(a["y"]) / 2
    north = (0, 0, 1, math.pi / 2)
    on_back = (0, 1, 0, -math.pi / 2)
    return [
        ("inside", (cx, cy, UPRIGHT_Z), north, True),
        ("just inside (5 cm)", (cx, a["y"][0] + 0.05, UPRIGHT_Z), north, True),
        ("just outside (5 cm)", (cx, a["y"][0] - 0.05, UPRIGHT_Z), north, False),
        ("earlier leg", EARLIER_LEG[world] + (UPRIGHT_Z,), (0, 0, 1, 0), False),
        ("fallen inside", (cx, cy, LYING_Z), on_back, False),
    ]


def main():
    supervisor = Supervisor()
    step = int(supervisor.getBasicTimeStep())
    world = Path(supervisor.getWorldPath()).stem
    truth = GroundTruth(supervisor)
    nao = supervisor.getFromDef("NAO")
    results = []

    def run_for(seconds):
        for _ in range(int(seconds * 1000 / step)):
            if supervisor.step(step) == -1:
                raise SystemExit
            truth.step()

    for i, (label, position, rotation, expected) in enumerate(cases(world)):
        run_id = "selftest-%s-%d" % (world, i)
        folder = truth.directory / run_id
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("t0", "rest_received", "end", "ground_truth.json"):
            (folder / name).unlink(missing_ok=True)
        (truth.directory / "run_id").write_text(run_id)
        nao.getField("translation").setSFVec3f(list(position))
        nao.getField("rotation").setSFRotation(list(rotation))
        nao.resetPhysics()
        run_for(0.2)
        (folder / "t0").write_text("selftest")       # the runner's t0 signal
        run_for(2.0)                                  # let the robot settle
        (folder / "rest_received").write_text("selftest")  # what the bridge writes when rest arrives
        run_for(0.1)
        (folder / "end").write_text("selftest")      # the runner's end signal
        run_for(0.1)
        ground_truth = json.loads((folder / "ground_truth.json").read_text())
        v = success.verdict(world, ground_truth)
        results.append({"label": label, "xy": ground_truth["rest"]["torso"][:2], "success": v["success"],
                        "expected": expected, "torso": ground_truth["rest"]["torso"],
                        "tilt_deg": ground_truth["rest"]["tilt_deg"], "upright": ground_truth["rest"]["upright"],
                        "rtf": ground_truth["real_time_factor"], "reason": v["reason"]})
        print("%-18s %-22s torso=(%.3f, %.3f, %.3f) tilt=%5.1f upright=%-5s rtf=%s -> success=%-5s expected=%-5s %s"
              % (world, label, *ground_truth["rest"]["torso"], ground_truth["rest"]["tilt_deg"],
                 ground_truth["rest"]["upright"], ground_truth["real_time_factor"], v["success"], expected,
                 "OK" if v["success"] == expected else "WRONG"), flush=True)
    (truth.directory / "run_id").unlink(missing_ok=True)
    (truth.directory / ("results-%s.json" % world)).write_text(json.dumps(results, indent=2))
    # The NAO is left where the last case put it; the next run's reset reloads the world.


if __name__ == "__main__":
    main()
