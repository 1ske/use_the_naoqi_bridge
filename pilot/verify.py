"""Instruction and image check (section 5.1.5): does the server's instruction text, and does a tool result's image,
reach the agent's LLM?

    python3 pilot/verify.py check AGENT instructions|image --when before|after [--repeat 3]   (real LLM: costs)
    python3 pilot/verify.py all --when before|after [--agents A1,A2,...]                     (real LLM: costs)
    python3 pilot/verify.py standin [AGENT ...]      (free; default one agent per harness, config/checks.json)

`all` runs both checks, `repeat` (3) times each, for every agent (default all eight). --when records whether the check
is the one before the pilot or the one after the last run (section 5.1.5).

Each attempt starts a fresh container from the agent's verification build (naoqi-pilot/<harness>-verify: the harness
image with pilot/verify/naoqi_mcp_verify.py, so no robot or bridge is involved), with the agent's login, proxy
allowlist and settings, and stages a new random word and a new random six-digit code into it:
  * instructions: the word is one line of the server's instructions; the prompt asks for it without calling a tool;
  * image: the code is the image in get_state's result; the prompt asks the agent to call get_state and report it.
An attempt passes if the agent's final answer contains the word or the code. The check passes if all attempts pass.
Results go to ~/.naoqi-pilot/grid/checks/<check_id>/ (result.json, the transcript, cost.json for OpenCode).

standin (free): the harness talks to pilot/tools/fake_llm.py instead, which cannot read anything; one image-check
attempt per agent then shows what the harness sent: whether the word (in the server's instructions) appears in a model
request, whether the code image does, and the model, reasoning and provider settings of every model request.
"""
import argparse
import base64
import datetime
import json
import os
import secrets
import shutil
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pilot"))
import metrics  # noqa: E402
import records  # noqa: E402
import run as runner  # noqa: E402
import transcript_check  # noqa: E402
from agent_container import AGENT_IDS, AGENTS, AgentContainer, docker  # noqa: E402

CHECKS = REPO / "pilot" / "config" / "checks.json"
OUT = records.CHECKS
TIMEOUT = 300  # s per attempt
WORDS = ["amber", "basalt", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper", "kelp",
         "lantern", "marble", "nimbus", "orchid", "pebble", "quartz", "raven", "saffron", "tundra", "umber", "velvet",
         "willow", "xenon", "yarrow", "zephyr", "anchor", "bramble", "cinder", "dune", "falcon", "glacier", "hazel",
         "iris", "jasper", "kestrel", "lichen", "meadow", "nectar", "opal"]

# 5x7 bitmap digits, drawn scaled into a 320x240 grayscale PNG (no image library in the images or on the Mac).
DIGITS = {"0": "01110100011001110101110011000101110", "1": "00100011000010000100001000010001110",
          "2": "01110100010000100010001000100011111", "3": "11110000010000101110000010000111110",
          "4": "00010001100101010010111110001000010", "5": "11111100001111000001000011000101110",
          "6": "00110010001000011110100011000101110", "7": "11111000010001000100010000100001000",
          "8": "01110100011000101110100011000101110", "9": "01110100011000101111000010001001100"}


def code_png(code, width=320, height=240, scale=7):
    rows = [[255] * width for _ in range(height)]
    glyph_w, gap = 5 * scale, 2 * scale
    x0 = (width - len(code) * glyph_w - (len(code) - 1) * gap) // 2
    y0 = (height - 7 * scale) // 2
    for i, digit in enumerate(code):
        bits = DIGITS[digit]
        for gy in range(7):
            for gx in range(5):
                if bits[gy * 5 + gx] == "1":
                    for y in range(y0 + gy * scale, y0 + (gy + 1) * scale):
                        for x in range(x0 + i * (glyph_w + gap) + gx * scale,
                                       x0 + i * (glyph_w + gap) + (gx + 1) * scale):
                            rows[y][x] = 0
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    chunk = lambda tag, data: (struct.pack(">I", len(data)) + tag + data
                               + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def final_answer(harness, agent_dir):
    events = metrics.read_jsonl_lenient(agent_dir / "transcript.jsonl")
    if harness == "claude-code":
        results = [e for e in events if e.get("type") == "result"]
        return (results[-1].get("result") or "") if results else ""
    if harness == "opencode":
        return " ".join((e.get("part") or {}).get("text", "") for e in events if e.get("type") == "text")
    if harness == "agy":
        results = [e["result"] for e in events if e.get("event") == "result"]
        return (results[-1].get("response") or "") if results else ""
    raise ValueError(harness)


def fake_script(harness, check):
    """What the fake LLM does: call get_state (and open the offloaded image in agy) for the image check."""
    if check == "instructions":
        return {"steps": [{"text": "No answer (fake LLM)."}]}
    steps = [{"tool": "nao:get_state", "args": {"toolSummary": "Robot state check"} if harness == "agy" else {}}]
    if harness == "agy":
        steps.append({"tool": "view_file", "args": {"AbsolutePath": "$OFFLOADED", "toolAction": "View image",
                                                    "toolSummary": "Camera image view"}})
    return {"steps": steps + [{"text": "No answer (fake LLM)."}]}


def reached_model(cap, word, png):
    """For --fake-llm: did the word, and did the code image, appear in any model request the harness sent?"""
    requests = [json.loads(line) for line in (cap / "requests.jsonl").read_text().splitlines() if line.strip()]
    bodies = [json.dumps(r.get("body")) for r in requests if r.get("api") != "other"]
    b64 = base64.b64encode(png).decode()
    return {"word_in_a_request": any(word in b for b in bodies),
            "code_image_in_a_request": any(b64[:400] in b for b in bodies),
            "model_requests": len(bodies),
            "request_settings": sorted({json.dumps(request_settings(r), sort_keys=True) for r in requests
                                        if r.get("api") not in ("other", "get")})}


def request_settings(request):
    """The model, reasoning and provider settings of one recorded model request (and whether it offered tools:
    requests without tools are the harness's side requests, such as the session title)."""
    body, api = request.get("body") or {}, request.get("api")
    keep = {"anthropic": ("model", "output_config", "thinking"), "openai": ("model", "reasoning", "provider"),
            "gemini": ()}[api]
    out = {k: body.get(k) for k in keep if k in body}
    if api == "gemini":
        out["model"] = request.get("path", "").split("/models/")[-1].split(":")[0]
        out["thinkingConfig"] = (body.get("generationConfig") or {}).get("thinkingConfig")
    out["api"], out["side_request"] = api, request.get("label") == "side"
    return out


SERIES = {"defer_cost": False, "floor": 0.0}  # verify.py all: OpenRouter usage settled once for the whole series


def attempt(agent, check, fake, check_dir, when=None):
    spec, config = AGENTS[agent], json.loads(CHECKS.read_text())
    harness = spec["harness"]
    word = "%s-%s" % (secrets.choice(WORDS), secrets.choice(WORDS))
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    png = code_png(code)
    attempt_id = "%s-%s-%s%s" % (agent, check, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                                 "-test" if fake else "")
    folder = check_dir / attempt_id
    folder.mkdir(parents=True)
    image = "%s-verify" % spec["image"]
    container = AgentContainer("chk-" + attempt_id.lower(), agent, folder / "agent", image=image,
                               test_env=runner.fake_llm_env(harness) if fake else None,
                               proxy_allow=[r"^no-host-allowed-in-fake-llm-tests\.invalid$"] if fake else None)
    result = {"check": check, "agent": agent, "when": "standin" if fake else when, "attempt_id": attempt_id,
              "fake_llm": fake, "model": spec["model"], "expected": None,
              "prompt": config["prompts"][check], "image": {"name": image}, "system_commit": runner.git("rev-parse", "HEAD"),
              "system_uncommitted_changes": bool(runner.git("status", "--porcelain")),
              "harness_version": runner.LOCK["harness_versions"][runner.HARNESS_KEY[harness]]}
    result["expected"] = word if check == "instructions" else code
    usage_before = None
    try:
        if harness == "agy" and not fake and not runner.agy_login_ok("chk-" + attempt_id.lower()):
            raise RuntimeError("the agy login has expired (agy models failed)")
        container.setup_network()
        if fake:
            script = Path(tempfile.mkdtemp()) / "script.json"
            script.write_text(json.dumps(fake_script(harness, check)))
            cap = runner.start_fake_llm(container, script)
        elif harness == "opencode":
            container.openrouter_key = runner.OPENROUTER_KEY.read_text().strip()
            usage_before = runner.openrouter_usage(container.openrouter_key)
        result["image"]["id"] = container.create(config["prompts"][check])
        if fake and harness == "agy":
            runner.stage_agy_fake_settings(container)
        with tempfile.TemporaryDirectory() as stage:
            (Path(stage) / "word").write_text(word + "\n")
            (Path(stage) / "code.png").write_bytes(png)
            os.chmod(stage, 0o755)
            docker("cp", stage + "/.", "%s:/verify" % container.name)
        container.start()
        deadline = time.time() + TIMEOUT
        while container.running() and time.time() < deadline:
            time.sleep(1)
        result["timed_out"] = container.running()
        container.stop()
        container.collect()
        result["answer"] = final_answer(harness, folder / "agent")
        result["tool_calls"] = transcript_check.check(agent, folder / "agent")
        if fake:
            result["reached_model"] = reached_model(cap, word, png)
            shutil.copy(cap / "requests.jsonl", folder / "fake_llm_requests.jsonl")
        if usage_before is not None and SERIES["defer_cost"]:
            floor = 0.9 * max(runner.opencode_step_costs(folder / "agent"),
                              runner.opencode_token_floor(folder / "agent", spec["model"])[0])
            SERIES["floor"] += floor
            (folder / "cost.json").write_text(json.dumps({
                "source": "not settled per attempt: the series' OpenRouter usage is settled once at its end "
                          "(series-cost-*.json in the checks folder); the attempt's own costs as OpenCode logged them",
                "settled": False, "waited_s": 0, "settle_floor_usd": round(floor, 8),
                "cross_check_step_costs_usd": runner.opencode_step_costs(folder / "agent")}, indent=2))
        elif usage_before is not None:
            floor = 0.9 * max(runner.opencode_step_costs(folder / "agent"),
                              runner.opencode_token_floor(folder / "agent", spec["model"])[0])
            usage_after, waited, settled = runner.settled_usage(container.openrouter_key, usage_before, floor)
            (folder / "cost.json").write_text(json.dumps({
                "source": "OpenRouter /api/v1/key usage of the shared key (A2, A4, A6, A8), after minus before the attempt",
                "usage_before_usd": usage_before, "usage_after_usd": usage_after,
                "cost_usd": round(usage_after - usage_before, 8), "settled": settled, "waited_s": waited,
                "cross_check_step_costs_usd": runner.opencode_step_costs(folder / "agent")}, indent=2))
        price = dict(runner.PRICES[runner.PRICED_MODEL[harness]], model=runner.PRICED_MODEL[harness]) \
            if harness in runner.PRICED_MODEL else None
        result["cost_usd"], result["cost_source"] = metrics.harness_cost(harness, folder / "agent", price)
        result["passed"] = None if fake else result["expected"].lower() in result["answer"].lower()
    except BaseException as e:
        result["error"] = repr(e)
        result["passed"] = None
        raise
    finally:
        container.remove()
        (folder / "result.json").write_text(json.dumps(result, indent=2))
    return result


def check_series(agent, check, when, repeat, fake=False):
    if not fake and not AGENTS[agent].get("model_id_confirmed"):
        raise SystemExit("%s: the model ID %r is not confirmed by the author (pilot/config/agents.json)"
                         % (agent, AGENTS[agent]["model"]))
    check_dir = OUT / ("%s-%s-%s-%s" % (agent, check, "standin" if fake else when,
                                         datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    results = []
    for _ in range(repeat):
        r = attempt(agent, check, fake, check_dir, when)
        results.append(r)
        print(json.dumps({k: r.get(k) for k in ("attempt_id", "expected", "passed", "cost_usd")}
                         | {"reached_model": {k: v for k, v in (r.get("reached_model") or {}).items()
                                              if k != "request_settings"}}
                         | {"answer": (r.get("answer") or "")[:120]}), flush=True)
    summary = {"agent": agent, "check": check, "when": "standin" if fake else when, "fake_llm": fake,
               "attempts": len(results), "passed": None if fake else all(r["passed"] for r in results)}
    if fake:
        summary["reached_model"] = results[-1].get("reached_model")
    (check_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2 if fake else None))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    attempts = json.loads(CHECKS.read_text())["attempts"]
    p = sub.add_parser("check", help="one check for one agent (real LLM)")
    p.add_argument("agent", choices=AGENT_IDS)
    p.add_argument("check", choices=["instructions", "image"])
    p.add_argument("--when", choices=["before", "after"], required=True)
    p.add_argument("--repeat", type=int, default=attempts)
    p = sub.add_parser("all", help="both checks for every agent (real LLMs)")
    p.add_argument("--when", choices=["before", "after"], required=True)
    p.add_argument("--agents", default=",".join(AGENT_IDS))
    p.add_argument("--repeat", type=int, default=attempts)
    p = sub.add_parser("standin", help="free: what each harness sends (stand-in LLM)")
    p.add_argument("agents", nargs="*", metavar="AGENT", help="default: one per harness")
    args = parser.parse_args()
    if args.cmd == "check":
        check_series(args.agent, args.check, args.when, args.repeat)
    elif args.cmd == "all":
        agents = args.agents.split(",")
        for agent in agents:  # refuse before anything is spent
            if agent not in AGENT_IDS or not AGENTS[agent].get("model_id_confirmed"):
                raise SystemExit("%s: unknown agent or model ID not confirmed" % agent)
        key = runner.OPENROUTER_KEY.read_text().strip() \
            if any(AGENTS[a]["harness"] == "opencode" for a in agents) else None
        if key:
            SERIES["defer_cost"] = True
            series_before = runner.openrouter_usage(key)
        summaries = [check_series(a, c, args.when, args.repeat) for a in agents for c in ("instructions", "image")]
        print(json.dumps({"%s %s" % (s["agent"], s["check"]): s["passed"] for s in summaries}, indent=2))
        if key:
            # Settle once, so that no usage of the checks can fall into a later run's window on the shared key.
            print("settling the series' OpenRouter usage", flush=True)
            after, waited, settled = runner.settled_usage(key, series_before, SERIES["floor"])
            out = OUT / ("series-cost-%s-%s.json" % (args.when, datetime.datetime.now(datetime.timezone.utc)
                                                       .strftime("%Y%m%dT%H%M%SZ")))
            out.write_text(json.dumps({"agents": agents, "usage_before_usd": series_before, "usage_after_usd": after,
                                       "cost_usd": round(after - series_before, 8), "settled": settled,
                                       "waited_s": waited, "settle_floor_usd": round(SERIES["floor"], 8)}, indent=2))
            print(out.read_text())
    else:
        agents = args.agents or list(json.loads(CHECKS.read_text())["standin_agent_per_harness"].values())
        if set(agents) - set(AGENT_IDS):
            parser.error("unknown agents: %s" % ", ".join(sorted(set(agents) - set(AGENT_IDS))))
        for agent in agents:
            check_series(agent, "image", None, 1, fake=True)


if __name__ == "__main__":
    main()
