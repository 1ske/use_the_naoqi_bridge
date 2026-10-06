"""Ground-truth supervisor for the simulated tasks (Webots controller of the `truth` robot, runs on the Mac):

    WEBOTS_HOME=/Applications/Webots.app /Applications/Webots.app/Contents/MacOS/webots-controller \
        --robot-name=truth pilot/supervisor/pilot_supervisor.py

It records only what section 5.1 asks for, into <instrumentation>/<run_id>/ground_truth.json:
  * rest: the torso position and whether the robot is upright, at the moment the bridge receives the run's
    first rest. The bridge creates <run_id>/rest_received first thing when that call arrives, before the robot
    stops or crouches; this controller checks for the file every simulation step (20 ms) and takes the pose on
    the step it appears. (The bridge's own timestamps come from the Docker VM's clock, which was measured to
    wander up to about 1 s from the Mac's, so a later lookup by time would not be precise enough.)
  * real_time_factor: simulated time / Mac wall-clock time between the runner's <run_id>/t0 and <run_id>/end
    signals (the moment the prompt was submitted, and the end of the run).
The current run is the one named in <instrumentation>/run_id (written by the runner). The record is saved as soon
as a run starts, with the open world and whether Webots runs in real-time mode, which the runner checks before t0.
When the runner removes run_id at the end of the run, the supervisor keeps watching that run's folder for `end`.
"""
import json
import math
import os
import time
from pathlib import Path

INSTRUMENTATION = Path(os.environ.get("PILOT_INSTRUMENTATION_DIR", Path.home() / ".naoqi-pilot" / "instrumentation"))
CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config" / "worlds.json").read_text())
MAX_TILT_DEG = CONFIG["upright"]["max_tilt_deg"]


class GroundTruth:
    def __init__(self, supervisor, directory=INSTRUMENTATION):
        self.supervisor = supervisor
        self.nao = supervisor.getFromDef("NAO")
        self.directory = Path(directory)
        self.run_id = None
        self.record = {}
        self.marks = {}

    def current_run(self):
        try:
            run_id = (self.directory / "run_id").read_text().strip()
        except OSError:
            return None
        return run_id if run_id and all(c.isalnum() or c in "-_." for c in run_id) else None

    def torso(self):
        """Torso position (m, world frame) and the angle between the torso's up axis and vertical (deg)."""
        x, y, z = self.nao.getPosition()
        up_z = self.nao.getOrientation()[8]  # z component of the torso's z axis (row-major 3x3)
        return [x, y, z], math.degrees(math.acos(max(-1.0, min(1.0, up_z))))

    def save(self):
        folder = self.directory / self.run_id
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "ground_truth.json.tmp"
        tmp.write_text(json.dumps(self.record, indent=2))
        tmp.replace(folder / "ground_truth.json")

    def step(self):
        """Call once per simulation step."""
        run_id = self.current_run()
        if run_id and run_id != self.run_id:  # a new run: start from scratch
            self.run_id, self.marks = run_id, {}
            self.record = {"run_id": run_id, "world": Path(self.supervisor.getWorldPath()).name,
                           "real_time_mode": self.supervisor.simulationGetMode() == self.supervisor.SIMULATION_MODE_REAL_TIME,
                           "rest": None, "real_time_factor": None}
            self.save()
        run_id = self.run_id
        if not run_id:
            return
        folder = self.directory / run_id
        if self.record["rest"] is None and (folder / "rest_received").exists():
            position, tilt = self.torso()
            self.record["rest"] = {"torso": [round(v, 4) for v in position], "tilt_deg": round(tilt, 2),
                                   "upright": tilt <= MAX_TILT_DEG}
            self.save()
        if "t0" not in self.marks and (folder / "t0").exists():
            self.marks["t0"] = (self.supervisor.getTime(), time.time())
        if "t0" in self.marks and self.record["real_time_factor"] is None and (folder / "end").exists():
            sim0, wall0 = self.marks["t0"]
            sim1, wall1 = self.supervisor.getTime(), time.time()
            self.record["real_time_factor"] = round((sim1 - sim0) / (wall1 - wall0), 4) if wall1 > wall0 else None
            self.save()


def main():
    from controller import Supervisor
    supervisor = Supervisor()
    step = int(supervisor.getBasicTimeStep())
    truth = GroundTruth(supervisor)
    print("pilot supervisor: watching %s" % truth.directory, flush=True)
    while supervisor.step(step) != -1:
        truth.step()


if __name__ == "__main__":
    main()
