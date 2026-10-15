"""Run order (section 5.1.7): randomized complete blocks with a recorded seed.

    python3 pilot/order.py new PHASE TASK --blocks N [--seed S]   -> ~/.naoqi-pilot/grid/PHASE/orders/TASK.json
    python3 pilot/order.py status PHASE TASK                      -> every position, its attempts and their validity
    python3 pilot/order.py verify PHASE TASK                      -> redraws the order from its seed and compares

Each block has one run of every agent on one task, in an order drawn with random.Random(seed).sample, one draw per
block. The seed is drawn with secrets.randbits(64) unless given, and is stored in the order file with the Python
version, so that the order can be redrawn. The order file is never changed after it is written.

The runner (pilot/run.py next) takes the first position that has no valid run yet. An invalid run is repeated in the
same position; every attempt keeps its own run folder, and its manifest records the position and the attempt number.
A position counts as done when one of its attempts is valid (pilot/metrics.py) and not excluded
(config/run_exclusions.json).
"""
import argparse
import datetime
import json
import platform
import random
import secrets
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import records  # noqa: E402
from agent_container import AGENT_IDS  # noqa: E402

WORLDS = json.loads((REPO / "pilot" / "config" / "worlds.json").read_text())


def draw(seed, agents, blocks):
    rng = random.Random(seed)
    order = []
    for block in range(1, blocks + 1):
        for agent in rng.sample(agents, len(agents)):
            order.append({"position": len(order) + 1, "block": block, "agent": agent})
    return order


def new(phase, task, blocks, seed=None):
    if task not in WORLDS["worlds"]:
        raise SystemExit("unknown task %r" % task)
    path = records.order_file(phase, task)
    if path.exists():
        raise SystemExit("%s exists; an order is never redrawn or changed" % path)
    seed_source = "given on the command line" if seed is not None else "secrets.randbits(64)"
    seed = secrets.randbits(64) if seed is None else seed
    order = {
        "phase": phase, "task_key": task, "agents": AGENT_IDS, "blocks": blocks, "seed": seed,
        "seed_source": seed_source,
        "method": "randomized complete blocks: per block, random.Random(seed).sample(agents, len(agents)), "
                  "blocks drawn one after the other from the same generator",
        "python": platform.python_version(),
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "system_commit": subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                                        text=True).stdout.strip(),
        "order": draw(seed, AGENT_IDS, blocks),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(order, indent=2) + "\n")
    return path


def load(phase, task):
    path = records.order_file(phase, task)
    if not path.exists():
        raise SystemExit("no run order for %s/%s: draw one with pilot/order.py new %s %s --blocks N"
                         % (phase, task, phase, task))
    return json.loads(path.read_text())


def attempts(phase, task):
    """{position: [{"run_id", "attempt", "valid", "excluded", "invalid_reasons", "end_condition"}]}, from the
    manifests of the phase's runs."""
    import metrics  # here, so that drawing an order does not need the analysis's imports
    out = {}
    for run_dir in records.runs_of(phase):
        manifest = json.loads((run_dir / "manifest.json").read_text())
        ro = manifest.get("run_order") or {}
        if manifest.get("task_key") != task or ro.get("position") is None:
            continue
        try:
            m = metrics.metrics(run_dir)
            valid, excluded, reasons = m["valid"], m["excluded"], m["invalid_reasons"]
        except Exception as e:  # noqa: BLE001 - a run whose record cannot be read is not valid
            valid, excluded, reasons = False, None, ["record unreadable: %r" % e]
        out.setdefault(ro["position"], []).append({
            "run_id": manifest["run_id"], "agent": manifest["agent"], "attempt": ro.get("attempt"),
            "valid": valid and not excluded, "excluded": excluded, "invalid_reasons": reasons,
            "end_condition": manifest.get("end_condition")})
    for runs in out.values():
        runs.sort(key=lambda r: (r["attempt"] or 0, r["run_id"]))
    return out


def status(phase, task):
    order = load(phase, task)
    tried = attempts(phase, task)
    rows = []
    for slot in order["order"]:
        runs = tried.get(slot["position"], [])
        wrong = [r["run_id"] for r in runs if r["agent"] != slot["agent"]]
        if wrong:
            raise SystemExit("position %d is %s, but these runs there are of another agent: %s"
                             % (slot["position"], slot["agent"], ", ".join(wrong)))
        rows.append(dict(slot, attempts=runs, done=any(r["valid"] for r in runs)))
    return order, rows


def next_slot(phase, task):
    """The first position without a valid run, with the number of its next attempt; None when all are done."""
    order, rows = status(phase, task)
    for row in rows:
        if not row["done"]:
            return dict(row, attempt=len(row["attempts"]) + 1, order_seed=order["seed"])
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("phase", choices=records.PHASES)
    p.add_argument("task", choices=list(WORLDS["worlds"]))
    p.add_argument("--blocks", type=int, required=True, help="runs per agent on this task")
    p.add_argument("--seed", type=int)
    for name in ("status", "verify"):
        p = sub.add_parser(name)
        p.add_argument("phase", choices=records.PHASES)
        p.add_argument("task", choices=list(WORLDS["worlds"]))
    args = parser.parse_args()
    if args.cmd == "new":
        print(new(args.phase, args.task, args.blocks, args.seed))
    elif args.cmd == "verify":
        order = load(args.phase, args.task)
        same = draw(order["seed"], order["agents"], order["blocks"]) == order["order"]
        print("the order file matches its seed" if same else "MISMATCH: the order file does not match its seed")
        sys.exit(0 if same else 1)
    else:
        order, rows = status(args.phase, args.task)
        print("%s / %s: seed %d, %d blocks, %d of %d positions done" % (
            args.phase, args.task, order["seed"], order["blocks"], sum(r["done"] for r in rows), len(rows)))
        for r in rows:
            tries = "; ".join("%s #%s %s%s" % (a["run_id"], a["attempt"], "valid" if a["valid"] else "INVALID",
                                               "" if a["valid"] else " (%s)" % "; ".join(a["invalid_reasons"]
                                                                                        or [a["excluded"] or ""]))
                              for a in r["attempts"]) or "-"
            print("%3d  block %d  %s  %s  %s" % (r["position"], r["block"], r["agent"],
                                                 "done" if r["done"] else "open", tries))


if __name__ == "__main__":
    main()
