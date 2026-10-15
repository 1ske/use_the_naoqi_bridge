"""Scriptable fake LLM for free, offline harness checks (not used in runs).

Speaks three APIs, streaming or not:
  Gemini      POST .../models/<model>:streamGenerateContent | :generateContent   (agy with GOOGLE_GEMINI_BASE_URL)
  Anthropic   POST /v1/messages                                                 (Claude Code with ANTHROPIC_BASE_URL)
  OpenAI      POST .../chat/completions                                         (OpenCode's OpenRouter provider)

A tool step's name "nao:TOOL" becomes the offered tool whose name ends in TOOL (mcp__naoqi__TOOL in Claude Code,
naoqi_TOOL in OpenCode, mcp_naoqi_TOOL in agy), so one script serves all three harnesses.
A step {"error": "provider" | "usage_limit"} makes this and every later main request fail with that API's error
response (provider: HTTP 500; usage_limit: HTTP 429, for Anthropic with the subscription's unified rate-limit headers
"rejected"), so that the harness's retries and its final error message can be checked (instrumentation tests only).

Every request is recorded to /cap/requests.jsonl. In a step's args, the string "$OFFLOADED" is replaced by the path of
the last image agy offloaded to a file ("[Resource offloaded to file://PATH]" in the request). A request that offers tools is a "main" request and gets the
next step of /cap/script.json: {"steps": [{"tool": NAME, "args": {...}} | {"text": "..."}]}; after the last step
it answers with text. A request without tools (title or other side request) gets the text "Side reply".
Usage is distinctive so that a harness's reported totals show which responses it counted:
  main step i (1-based): input 1000*i, output 10*i, thinking 1*i;   side request: input 100000, output 7.
Gemini responses also report a tenth of the input as cached (cachedContentTokenCount, included in the input).
"""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET_HEADERS = {"authorization", "x-api-key", "x-goog-api-key", "cookie"}
state = {"main": 0, "error": None}


def script():
    try:
        return json.load(open("/cap/script.json"))["steps"]
    except (OSError, ValueError, KeyError):
        return []


def offered_tools(api, body):
    tools = body.get("tools") or []
    if api == "gemini":
        return [d.get("name") for t in tools for d in t.get("functionDeclarations") or []]
    if api == "openai":
        return [(t.get("function") or {}).get("name") for t in tools]
    return [t.get("name") for t in tools]


def with_tool_name(step, api, body):
    """Resolve "nao:TOOL" to the harness's name for the naoqi server's TOOL."""
    name = step.get("tool") or ""
    if not name.startswith("nao:"):
        return step
    tool = name[4:]
    match = [t for t in offered_tools(api, body) if t and t in ("mcp__naoqi__" + tool, "naoqi_" + tool,
                                                                   "mcp_naoqi_" + tool)]
    args = dict(step.get("args") or {})
    if api == "gemini":  # agy requires its own toolSummary argument on every tool call
        args.setdefault("toolSummary", "NAO %s" % tool)
    return dict(step, tool=match[0] if match else "naoqi_tool_not_offered_" + tool, args=args)


def with_offloaded(step, raw):
    """Replace "$OFFLOADED" in a tool step's args by the last offloaded image path in the request."""
    if "tool" not in step or "$OFFLOADED" not in json.dumps(step.get("args", {})):
        return step
    paths = re.findall(r"Resource offloaded to file://([^\]\\\"]+)", raw)
    path = paths[-1] if paths else "/no-offloaded-image"
    return dict(step, args=json.loads(json.dumps(step["args"]).replace("$OFFLOADED", path)))


def next_action(has_tools):
    if not has_tools:
        return {"text": "Side reply"}, {"input": 100000, "output": 7, "thinking": 0}, "side"
    if state["error"]:  # sticky: every later main request (also the harness's retries) fails the same way
        return {"error": state["error"]}, {}, "error"
    state["main"] += 1
    i = state["main"]
    steps = script()
    step = steps[i - 1] if i <= len(steps) else {"text": "Done."}
    if "error" in step:
        state["error"] = step["error"]
    return step, {"input": 1000 * i, "output": 10 * i, "thinking": i}, "main-%d" % i


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        path = self.path
        if "generateContent" in path or "streamGenerateContent" in path:
            has_tools = any(t.get("functionDeclarations") for t in body.get("tools") or [])
            api = "gemini"
        elif path.startswith("/v1/messages"):
            has_tools = bool(body.get("tools"))
            api = "anthropic"
        elif path.endswith("/chat/completions"):
            has_tools = bool(body.get("tools"))
            api = "openai"
        else:
            api, has_tools = "other", False
        action, usage, label = next_action(has_tools) if api != "other" else ({}, {}, "other")
        action = with_offloaded(with_tool_name(action, api, body), raw.decode("utf-8", "replace"))
        headers = {k: ("<redacted>" if k.lower() in SECRET_HEADERS else v) for k, v in self.headers.items()}
        with open("/cap/requests.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "label": label, "api": api, "path": path, "headers": headers,
                                "body": body, "reply": action, "usage": usage}) + "\n")
        if api == "other":
            return self.send_json(404, {"error": {"message": "not a model endpoint"}})
        if "error" in action:
            return self.error(api, action["error"])
        stream = body.get("stream", "streamGenerateContent" in path)
        getattr(self, api)(body, action, usage, stream)

    do_PUT = do_POST

    def do_GET(self):
        with open("/cap/requests.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "label": "other", "api": "get", "path": self.path}) + "\n")
        self.send_json(404, {"error": {"message": "not found"}})

    # --- transports -------------------------------------------------------------------------------------------
    def send_json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_sse(self, events):
        data = "".join(("event: %s\n" % name if name else "") + "data: %s\n\n" % (
            payload if isinstance(payload, str) else json.dumps(payload)) for name, payload in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, api, kind):
        """Each API's own error response for a provider error (500) or a usage limit (429)."""
        usage_limit = kind == "usage_limit"
        code, headers = (429 if usage_limit else 500), {}
        if api == "anthropic":
            payload = {"type": "error", "error": {"type": "rate_limit_error" if usage_limit else "api_error",
                                                  "message": "Rate limited" if usage_limit else "Internal server error"}}
            if usage_limit:  # what the subscription's limit sends (Claude Code reads these headers)
                headers = {"anthropic-ratelimit-unified-status": "rejected",
                           "anthropic-ratelimit-unified-reset": str(int(time.time()) + 3 * 3600),
                           "anthropic-ratelimit-unified-representative-claim": "five_hour",
                           "anthropic-ratelimit-unified-5h-status": "rejected", "retry-after": "10800"}
        elif api == "openai":
            payload = {"error": {"code": code, "message": "Rate limit exceeded" if usage_limit
                                 else "Provider returned error", "metadata": {"provider_name": "Fake"}}}
        else:
            payload = {"error": {"code": code, "message": "Resource has been exhausted (e.g. check quota)."
                                 if usage_limit else "Internal error encountered.",
                                 "status": "RESOURCE_EXHAUSTED" if usage_limit else "INTERNAL"}}
        data = json.dumps(payload).encode()
        self.send_response(code)
        for k, v in dict(headers, **{"content-type": "application/json", "content-length": str(len(data))}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # --- APIs ------------------------------------------------------------------------------------------------
    def gemini(self, body, action, usage, stream):
        part = ({"functionCall": {"name": action["tool"], "args": action.get("args", {})}, "thoughtSignature": "c2ln"}
                if "tool" in action else {"text": action["text"]})
        payload = {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
                   "usageMetadata": {"promptTokenCount": usage["input"], "candidatesTokenCount": usage["output"],
                                     "thoughtsTokenCount": usage["thinking"],
                                     "cachedContentTokenCount": usage["input"] // 10,  # part of promptTokenCount
                                     "totalTokenCount": usage["input"] + usage["output"] + usage["thinking"]},
                   "modelVersion": self.path.split("/models/")[-1].split(":")[0]}
        if stream:
            self.send_sse([(None, payload)])
        else:
            self.send_json(200, payload)

    def anthropic(self, body, action, usage, stream):
        if "tool" in action:
            block = {"type": "tool_use", "id": "toolu_fake%d" % state["main"], "name": action["tool"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(action.get("args", {}))}
            stop = "tool_use"
            full = dict(block, input=action.get("args", {}))
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": action["text"]}
            stop = "end_turn"
            full = {"type": "text", "text": action["text"]}
        u = {"input_tokens": usage["input"], "output_tokens": usage["output"],
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        msg = {"id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
               "stop_reason": None, "stop_sequence": None}
        if not stream:
            return self.send_json(200, dict(msg, content=[full], stop_reason=stop, usage=u))
        self.send_sse([
            ("message_start", {"type": "message_start", "message": dict(msg, content=[], usage=dict(u, output_tokens=1))}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": usage["output"]}}),
            ("message_stop", {"type": "message_stop"}),
        ])

    def openai(self, body, action, usage, stream):
        base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": body.get("model")}
        u = {"prompt_tokens": usage["input"], "completion_tokens": usage["output"],
             "total_tokens": usage["input"] + usage["output"],
             "completion_tokens_details": {"reasoning_tokens": usage["thinking"]}, "cost": 0.001 * state["main"]}
        if "tool" in action:
            call = {"index": 0, "id": "call_fake%d" % state["main"], "type": "function",
                    "function": {"name": action["tool"], "arguments": json.dumps(action.get("args", {}))}}
            delta, finish, message = ({"role": "assistant", "tool_calls": [call]}, "tool_calls",
                                      {"role": "assistant", "content": None, "tool_calls": [call]})
        else:
            delta, finish, message = ({"role": "assistant", "content": action["text"]}, "stop",
                                      {"role": "assistant", "content": action["text"]})
        if not stream:
            return self.send_json(200, dict(base, object="chat.completion",
                                            choices=[{"index": 0, "message": message, "finish_reason": finish}], usage=u))
        self.send_sse([
            (None, dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}])),
            (None, dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish}])),
            (None, dict(base, choices=[], usage=u)),
            (None, "[DONE]"),
        ])

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
