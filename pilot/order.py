"""Run order (section 5.1.7): randomized complete blocks with a recorded seed.

    python3 pilot/order.py new PHASE TASK --blocks N [--seed S]   -> ~/.naoqi-pilot/grid/PHASE/orders/TASK.json
    python3 pilot/order.py status PHASE TASK                      -> every position, its attempts and their validity
    python3 pilot/order.py verify PHASE TASK                      -> redraws the order from its seed and compares

Each block has one run of every agent on one task, in an order drawn with random.Random(seed).sample, one draw per
block. The seed is drawn with secrets.randbits(64) unless given, and is
stored in the order file with the Python version, so that the order can be redrawn.

    python3 pilot/order.py redraw-open PHASE TASK --keep-through N --reason TEXT

redraws the positions after N (which must all be open) with a new seed, keeping positions 1..N; the previous order
file is kept as TASK.rev<k>.json and the revision is recorded in the new file.

History: from 2026-10-15 12:21 until the end of that day's Corridor pilot, orders were drawn with a restriction
(no OpenCode run directly after another, also across blocks; METHOD_RESTRICTED), while all OpenCode agents shared one
OpenRouter key. With one key per OpenCode agent the restriction was dropped (author, 2026-10-15); `verify` still
checks order files drawn with it.

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
from agent_container import AGENT_IDS, AGENTS  # noqa: E402

WORLDS = json.loads((REPO / "pilot" / "config" / "worlds.json").read_text())
TASKS = list(WORLDS["worlds"]) + ["search"]  # search: the physical NAO (pilot/search.py)


OPENROUTER = {a for a in AGENT_IDS if AGENTS[a]["harness"] == "opencode"}
METHOD_RESTRICTED = ("randomized complete blocks, restricted: per block, random.Random(seed).sample of the block's agents "
                     "not yet placed, drawn again while an OpenCode agent directly follows another (also across blocks); "
                     "blocks drawn one after the other from the same generator")
METHOD = ("randomized complete blocks: per block, random.Random(seed).sample of the block's agents not yet placed; "
          "blocks drawn one after the other from the same generator")


def allowed(previous, sequence):
    """No OpenCode run directly after another (previous: the agent before the sequence, or None)."""
    chain = ([previous] if previous else []) + list(sequence)
    return not any(a in OPENROUTER and b in OPENROUTER for a, b in zip(chain, chain[1:]))


def draw_unrestricted(seed, agents, blocks):
    """The method before 2026-10-15 (no restriction), to verify order files drawn with it."""
    rng = random.Random(seed)
    order = []
    for block in range(1, blocks + 1):
        for agent in rng.sample(agents, len(agents)):
            order.append({"position": len(order) + 1, "block": block, "agent": agent})
    return order


def draw(seed, agents, blocks, fixed=(), restricted=False):
    """The order: fixed (a list of {"position", "block", "agent"}) first, then the rest of its block and the following
    blocks (with restricted: no OpenCode run directly after another; only to verify orders drawn so)."""
    rng = random.Random(seed)
    order = [dict(x) for x in fixed]
    first_block = order[-1]["block"] if order else 1
    for block in range(first_block, blocks + 1):
        placed = [x["agent"] for x in order if x["block"] == block]
        rest = [a for a in agents if a not in placed]
        previous = order[-1]["agent"] if order else None
        for _ in range(100000):
            candidate = rng.sample(rest, len(rest))
            if not restricted or allowed(previous, candidate):
                break
        else:
            raise RuntimeError("no allowed order for block %d" % block)
        for agent in candidate:
            order.append({"position": len(order) + 1, "block": block, "agent": agent})
    return order


def new(phase, task, blocks, seed=None):
    if task not in TASKS:
        raise SystemExit("unknown task %r" % task)
    path = records.order_file(phase, task)
    if path.exists():
        raise SystemExit("%s exists; an order is never redrawn or changed" % path)
    seed_source = "given on the command line" if seed is not None else "secrets.randbits(64)"
    seed = secrets.randbits(64) if seed is None else seed
    order = {
        "phase": phase, "task_key": task, "agents": AGENT_IDS, "blocks": blocks, "seed": seed,
        "seed_source": seed_source,
        "method": METHOD,
        "python": platform.python_version(),
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "system_commit": subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                                        text=True).stdout.strip(),
        "order": draw(seed, AGENT_IDS, blocks),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(order, indent=2) + "\n")
    return path


def redraw_open(phase, task, keep_through, reason, seed=None):
    """Keep positions 1..keep_through, redraw the rest under the restriction with a new seed (see the module doc)."""
    path = records.order_file(phase, task)
    old = load(phase, task)
    tried = attempts(phase, task)
    later = sorted(p for p in tried if p > keep_through)
    if later:
        raise SystemExit("positions %s already have runs; keep them" % later)
    k = 1
    while path.with_name("%s.rev%d.json" % (task, k)).exists():
        k += 1
    path.with_name("%s.rev%d.json" % (task, k)).write_text(path.read_text())
    seed_source = "given on the command line" if seed is not None else "secrets.randbits(64)"
    seed = secrets.randbits(64) if seed is None else seed
    fixed = old["order"][:keep_through]
    new_order = dict(old, seed=seed, seed_source=seed_source, method=METHOD, python=platform.python_version(),
                     order=draw(seed, old["agents"], old["blocks"], fixed))
    new_order["revisions"] = old.get("revisions", []) + [{
        "revision": k, "previous_file": path.with_name("%s.rev%d.json" % (task, k)).name,
        "previous_seed": old["seed"], "previous_method": old["method"], "kept_positions": keep_through,
        "reason": reason, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "system_commit": subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                                        text=True).stdout.strip()}]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(new_order, indent=2) + "\n")
    tmp.replace(path)  # atomic: a running `run.py next` reads either the old or the new file
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
    p.add_argument("task", choices=TASKS)
    p.add_argument("--blocks", type=int, required=True, help="runs per agent on this task")
    p.add_argument("--seed", type=int)
    p = sub.add_parser("redraw-open")
    p.add_argument("phase", choices=records.PHASES)
    p.add_argument("task", choices=TASKS)
    p.add_argument("--keep-through", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--seed", type=int)
    for name in ("status", "verify"):
        p = sub.add_parser(name)
        p.add_argument("phase", choices=records.PHASES)
        p.add_argument("task", choices=TASKS)
    args = parser.parse_args()
    if args.cmd == "new":
        print(new(args.phase, args.task, args.blocks, args.seed))
    elif args.cmd == "redraw-open":
        print(redraw_open(args.phase, args.task, args.keep_through, args.reason, args.seed))
    elif args.cmd == "verify":
        order = load(args.phase, args.task)
        kept = order["revisions"][-1]["kept_positions"] if order.get("revisions") else 0
        if order["method"] in (METHOD, METHOD_RESTRICTED):
            same = draw(order["seed"], order["agents"], order["blocks"], order["order"][:kept],
                        restricted=order["method"] == METHOD_RESTRICTED) == order["order"]
        else:
            same = draw_unrestricted(order["seed"], order["agents"], order["blocks"]) == order["order"]
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
