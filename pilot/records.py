"""Where the run records live, kept apart by study and phase (sections 5.1.7 and 5.1.8).

    ~/.naoqi-pilot/runs/, checks/      the first pilot (2026-10-05/07): void, old agent numbers; never read here
    ~/.naoqi-pilot/grid/<phase>/runs/<run_id>/     runs with the eight agents of the grid
    ~/.naoqi-pilot/grid/<phase>/orders/<task>.json the run order of a phase and task (pilot/order.py)
    ~/.naoqi-pilot/grid/checks/<check>/            instruction and image checks (pilot/verify.py)

Phases: "test" (runs made to develop and test the instrumentation, real or played by the stand-in LLM; never
analysed), "pilot" and "main". Every manifest also records its phase, and runs_of() returns a run only if both its
folder and its manifest say the phase asked for, so a run copied to the wrong folder is not picked up.
"""
import json
from pathlib import Path

PILOT_HOME = Path.home() / ".naoqi-pilot"
VOID_FIRST_PILOT = PILOT_HOME / "runs"  # never read by the analysis
STUDY = PILOT_HOME / "grid"
PHASES = ("test", "pilot", "main")
CHECKS = STUDY / "checks"


def runs_dir(phase):
    if phase not in PHASES:
        raise ValueError("unknown phase %r (one of %s)" % (phase, ", ".join(PHASES)))
    return STUDY / phase / "runs"


def order_file(phase, task):
    runs_dir(phase)  # validates the phase
    return STUDY / phase / "orders" / ("%s.json" % task)


def runs_of(phase, folder=None):
    """The run folders of a phase (default folder: runs_dir(phase)), sorted, whose manifest has that phase."""
    folder = Path(folder).expanduser() if folder else runs_dir(phase)
    out = []
    for manifest in sorted(folder.glob("*/manifest.json")):
        try:
            if json.loads(manifest.read_text()).get("phase") == phase:
                out.append(manifest.parent)
        except ValueError:
            continue
    return out
