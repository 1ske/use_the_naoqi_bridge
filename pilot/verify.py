"""Instruction and image check (section 5.1.5): does the server's instruction text, and does a tool result's image,
reach the agent's LLM?

    python3 pilot/verify.py AGENT instructions|image [--repeat 3] [--fake-llm]

Each attempt starts a fresh container from the agent's verification build (naoqi-pilot/<harness>-verify: the harness
image with pilot/verify/naoqi_mcp_verify.py, so no robot or bridge is involved), with the agent's login, proxy
allowlist and settings, and stages a new random word and a new random six-digit code into it:
  * instructions: the word is one line of the server's instructions; the prompt asks for it without calling a tool;
  * image: the code is the image in get_state's result; the prompt asks the agent to call get_state and report it.
An attempt passes if the agent's final answer contains the word or the code. The check passes if all attempts pass.
Results go to ~/.naoqi-pilot/checks/<check_id>/ (result.json, the transcript, cost.json for OpenCode).

--fake-llm (free): the harness talks to pilot/tools/fake_llm.py instead, which cannot read anything; the attempt then
checks what the harness sent: whether the word appears in a model request, and whether the code image does.
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
import run as runner  # noqa: E402
import transcript_check  # noqa: E402
from agent_container import AGENTS, AgentContainer, docker  # noqa: E402

CHECKS = REPO / "pilot" / "config" / "checks.json"
OUT = runner.PILOT_HOME / "checks"
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
    name = AGENTS["transcript_check"]["tool_name_format"][harness].format(tool="get_state")
    steps = [{"tool": name, "args": {"toolSummary": "Robot state check"} if harness == "agy" else {}}]
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
            "model_requests": len(bodies)}


def attempt(agent, check, fake, check_dir):
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
    result = {"check": check, "agent": agent, "attempt_id": attempt_id, "fake_llm": fake, "expected": None,
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
        if usage_before is not None:
            usage_after, waited, settled = runner.settled_usage(container.openrouter_key)
            (folder / "cost.json").write_text(json.dumps({
                "source": "OpenRouter /api/v1/key usage of the shared A1-A4 key, after minus before the attempt",
                "usage_before_usd": usage_before, "usage_after_usd": usage_after,
                "cost_usd": round(usage_after - usage_before, 8), "settled": settled, "waited_s": waited}, indent=2))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent", choices=[a for a in AGENTS if a.startswith("A")])
    parser.add_argument("check", choices=["instructions", "image"])
    parser.add_argument("--repeat", type=int, default=json.loads(CHECKS.read_text())["attempts"])
    parser.add_argument("--fake-llm", action="store_true")
    args = parser.parse_args()
    check_dir = OUT / ("%s-%s-%s" % (args.agent, args.check,
                                      datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    results = []
    for _ in range(args.repeat):
        r = attempt(args.agent, args.check, args.fake_llm, check_dir)
        results.append(r)
        print(json.dumps({k: r.get(k) for k in ("attempt_id", "expected", "passed", "reached_model", "cost_usd")}
                         | {"answer": (r.get("answer") or "")[:120]}), flush=True)
    summary = {"agent": args.agent, "check": args.check, "fake_llm": args.fake_llm, "attempts": len(results),
               "passed": None if args.fake_llm else all(r["passed"] for r in results)}
    (check_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
