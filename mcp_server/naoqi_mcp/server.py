"""MCP server (stdio) for the NAO: one tool per entry in src/primitives.json, forwarded to the bridge.

No NAOqi code and no safety logic lives here: the bridge (src/bridge.py) validates, clamps and
refuses. This process only describes the primitives to the LLM, forwards calls over HTTP and
sends heartbeats so the bridge knows its client is alive (without them it rests the robot).
Camera views in a result ("image" / "images", base64 JPEG; the bridge composes each result's frames
into one image) go out as MCP image content, so the model sees real images; the JSON text keeps
their metadata without the pixels.

Environment:
    NAOQI_BRIDGE_URL   default http://127.0.0.1:8765
    NAOQI_PRIMITIVES   default <repo>/src/primitives.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

BRIDGE_URL = os.environ.get("NAOQI_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
PRIMITIVES = Path(os.environ.get("NAOQI_PRIMITIVES",
                                 Path(__file__).resolve().parents[2] / "src" / "primitives.json"))
HEARTBEAT_PERIOD = 5.0  # s; the bridge rests the robot after BRIDGE_CLIENT_TIMEOUT (20 s) without requests
CALL_TIMEOUT = 400.0    # s; the bridge bounds every movement itself (go <= ~110 s)

INSTRUCTIONS = """\
Controls a NAO robot (simulated or real).
- Start with get_state, then stand.
- Every result includes a camera image (top: ahead, bottom: at the feet; yellow lines = ground
  distance in m). Use it to choose where to go; look_around shows the sides.
- go turns and walks; walk_to is for small corrections and sidesteps.
- Results say what actually happened (result, odometry, state): trust them over your plan.
  obstacle or bumped means something is in the way.
- say tells the people nearby what you're doing.
- rest when you are done, unsure, or after a fall that stand can't fix, and tell the human.
- battery is informational; mention it only below 10% and not charging.
"""


def load_primitives() -> list[dict]:
    return json.loads(PRIMITIVES.read_text())["primitives"]


def tool_for(entry: dict) -> types.Tool:
    read_only = entry["kind"] == "read"
    return types.Tool(
        name=entry["name"],
        description=entry["description"],
        input_schema=entry["params"],
        annotations=types.ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=False,
            idempotent_hint=read_only,
            open_world_hint=True,  # it moves a robot in the physical (or simulated) world
        ),
    )


def text_result(body: dict, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body, indent=2))],
        structured_content=body,
        is_error=is_error,
    )


def image_result(body: dict) -> types.CallToolResult:
    """Move "image" / "images" out of the JSON into image content blocks (metadata stays as text)."""
    body = dict(body)
    frames = ([body.pop("image")] if "image" in body else []) + body.pop("images", [])
    if not frames:
        return text_result(body, is_error=False)
    body["images_meta"] = [{key: value for key, value in frame.items() if key != "data"} for frame in frames]
    result = text_result(body, is_error=False)
    result.content += [types.ImageContent(type="image", data=frame["data"], mime_type=frame["mime_type"])
                       for frame in frames]
    return result


def build_server() -> Server:
    primitives = {entry["name"]: entry for entry in load_primitives()}
    client: httpx.AsyncClient | None = None

    async def heartbeat() -> None:
        while True:
            try:
                await client.post(f"{BRIDGE_URL}/heartbeat", timeout=3.0)
            except httpx.HTTPError:
                pass  # bridge down; calls will report it
            await asyncio.sleep(HEARTBEAT_PERIOD)

    @asynccontextmanager
    async def lifespan(_server: Server):
        nonlocal client
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
            task = asyncio.create_task(heartbeat())
            try:
                yield {}
            finally:
                task.cancel()

    async def list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[tool_for(entry) for entry in primitives.values()])

    async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in primitives:
            return text_result({"error": f"unknown tool {params.name!r}"}, is_error=True)
        try:
            response = await client.post(f"{BRIDGE_URL}/call/{params.name}", json=params.arguments or {})
            body = response.json()
        except httpx.HTTPError as e:
            return text_result({"error": f"robot bridge not reachable at {BRIDGE_URL} ({e!r}). "
                                         "Ask the human to start it: docker compose up bridge"}, is_error=True)
        except ValueError:
            return text_result({"error": f"bridge answered HTTP {response.status_code} without JSON"},
                               is_error=True)
        if body.get("ok"):
            return image_result(body["result"])
        return text_result({key: value for key, value in body.items() if key != "ok"}, is_error=True)

    return Server("naoqi", version="0.1.0", instructions=INSTRUCTIONS, lifespan=lifespan,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
