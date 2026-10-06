"""Verification build of the NAO MCP server, for the instruction and image check (section 5.1.5).

Installed only in the naoqi-pilot/<harness>-verify images, in place of the naoqi-mcp command; it runs the unchanged
server (mcp_server/naoqi_mcp/server.py) with two changes:
  * the server's instructions get one more line with the check's random word (/verify/word);
  * the bridge is replaced by a stand-in: get_state returns a fixed resting state whose image is the check's random
    code (/verify/code.png), heartbeats succeed, every other tool is refused. No robot or bridge is needed.
The runner (pilot/verify.py) stages /verify into the container before it starts.
"""
import base64
from pathlib import Path

import httpx

import naoqi_mcp.server as server

VERIFY = Path("/verify")
WORD = (VERIFY / "word").read_text().strip()
CODE_PNG = base64.b64encode((VERIFY / "code.png").read_bytes()).decode()

server.INSTRUCTIONS += "- Verification word: %s\n" % WORD

STATE = {"posture": "Crouch", "posture_family": "Crouch", "awake": False, "fallen": False, "fall_count": 0,
         "last_fall": None, "odometry": {"x": 0.0, "y": 0.0, "theta": 0.0}, "sonar_m": {"left": 2.55, "right": 2.55},
         "bumpers": {"left_foot": False, "right_foot": False}, "busy": None, "warnings": [],
         "battery": {"percent": 99.0, "charging": False}}


async def stand_in_bridge(self, url, *args, **kwargs):
    if url.endswith("/heartbeat"):
        status, body = 200, {"ok": True}
    elif url.endswith("/call/get_state"):
        status, body = 200, {"ok": True, "result": dict(STATE, images=[
            {"data": CODE_PNG, "mime_type": "image/png", "layout": "1 tile of 320x240 px"}])}
    else:
        status, body = 409, {"ok": False, "error": "refused: only get_state is available in this check"}
    return httpx.Response(status, json=body, request=httpx.Request("POST", url))


httpx.AsyncClient.post = stand_in_bridge

if __name__ == "__main__":
    server.main()
