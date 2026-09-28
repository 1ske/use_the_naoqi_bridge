# use_the_naoqi_bridge

Lets an LLM drive a NAO through NAOqi 2.1.4 over MCP. The LLM plans. The MCP server offers only
safe movement primitives (walk, turn, stop, postures, state) and the head cameras (as real MCP images),
and a Python 2.7 bridge enforces the safety rules. The simulator (naoqisim) comes from [NAOQI_ATTEMPT_v1](../NAOQI_ATTEMPT_v1). Everything
here talks to NAOqi on port 9559, so the same code runs on a real NAO V5 by changing `NAO_IP`.

```
LLM (Claude Code) ──MCP stdio──► mcp_server/  Python 3.10+, native on the Mac (uv), no NAOqi code
                                    │ HTTP/JSON, 127.0.0.1:8765  (+ heartbeat every 5 s)
                                    ▼
                                 bridge        Docker, Python 2.7 + pynaoqi, src/bridge.py; owns safety
                                    │ NAOqi tcp :9559 (host.docker.internal)
                                    ▼
          NAOQI_ATTEMPT_v1: naoqisim container (NAOqi 2.1.4)  ── or a real NAO V5
                                    │ tcp :1234 (extern controller "NAO")
                                    ▼
                                 Webots (Mac), webots/worlds/*.wbt
```

Why two processes: pynaoqi 2.1.4 exists only for Python 2.7 on Linux x86_64, so only the bridge
imports `naoqi`. `src/primitives.json` is the single list of primitives. The bridge validates
arguments against it, and the MCP server turns each entry into a tool.

## Run

1. Webots R2025a: open a world from `webots/worlds/` in real-time mode.
2. In NAOQI_ATTEMPT_v1: `docker compose up naoqisim`, wait for `NAOqi is ready`. Cameras are on by
   default. With `NAOQISIM_FLAGS=-nocam` everything still works, but results carry a "no camera image" warning instead of an image.
3. Here, the bridge: `docker compose up bridge` (add `-d` to run it in the background). It logs
   `connected to NAOqi ...` and the state of the protections.
4. Claude Code in this directory picks up `.mcp.json` and starts the MCP server (`naoqi`) itself.
   Check it with `/mcp`. Then ask it, for example, "stand up and walk 30 cm forward".

The MCP server is `uv run --directory mcp_server naoqi-mcp`, and uv installs its dependencies on
first run. `NAOQI_BRIDGE_URL` (default `http://127.0.0.1:8765`) points it at the bridge.

Connection check without the bridge: `docker compose run --rm app python connect.py`. It prints the
ALMotion version and doesn't move the robot.

Real robot: `NAO_IP=<robot ip> docker compose up bridge` (or `... run --rm app python connect.py`).

To switch worlds, stop naoqisim, open the other world, then start naoqisim again.

Ground truth (real position/tilt, not NAOqi's estimate), on the Mac:
```
WEBOTS_HOME=/Applications/Webots.app /Applications/Webots.app/Contents/MacOS/webots-controller \
    --robot-name=truth webots/tools/truth.py
```

### Talking to the bridge directly

```
curl localhost:8765/health
curl -XPOST localhost:8765/call/get_state -d '{}'
curl -XPOST localhost:8765/call/stand -d '{}'
curl -XPOST localhost:8765/call/walk_to -d '{"x":0.3,"y":0,"theta":0}'
curl -XPOST localhost:8765/call/stop -d '{}'
curl -XPOST localhost:8765/call/rest -d '{}'
```
Replies are `{"ok": true, "result": {...}}`, or `{"ok": false, "error": "...", "state": {...}}` with
400 (bad arguments), 409 (refused by a safety rule, or busy), 502 (NAOqi error) or 503 (NAOqi
unreachable). The bridge rests the robot if it gets no request for 20 s while the robot is awake
(`BRIDGE_CLIENT_TIMEOUT`). Plain curl sessions need to be quick, or run a heartbeat loop:
`while true; do curl -s -XPOST localhost:8765/heartbeat >/dev/null; sleep 3; done`.

## Primitives

| tool | kind | what it does |
|---|---|---|
| `get_state` | read | posture + family, fallen, odometry x/y/θ, awake, battery %, sonar L/R, foot bumpers, current movement, warnings |
| `look(camera)` | read | one JPEG: the `top` view above the `bottom` view (320×240 each), or just one camera, plus head yaw/pitch and state |
| `look_around(yaw?, pitch?)` | move | turns the head (joint speed ≤ 0.3, yaw ±2.0, pitch −0.6…0.5 rad), then faces forward; one panorama (top row = top camera, bottom row = bottom camera). No yaw: 5-direction sweep (≈ ±150°) |
| `scan` | move | the `look_around` sweep (head tilted down to the sides) plus measurements: `clearance` (free floor in m per 10° bearing, ±150°), `openings`, `colors` (bearing + distance of every clearly coloured thing), one panorama |
| `stand` | move | `wakeUp()` + `goToPosture("StandInit", 0.5)`; the only way to clear a fall |
| `sit` | move | `goToPosture("Sit", 0.5)` (tips over in the simulator, see below) |
| `walk_to(x, y, theta)` | move | relative `moveTo`, run with `.post`; `y` alone sidesteps; clamped to 1.0 m and π rad per call; stops at obstacles (below); returns odometry before/after/delta |
| `go(bearing_deg, distance_m?)` | move | turns, then walks up to 3 m with `moveToward`, re-measuring the floor ahead 5×/s: stops 0.3 m before anything in its 0.4 m lane, steers around lane edges (±35°), centres between side walls (`arrived`, `blocked`, `obstacle`, `bumped`, `blind`, …) |
| `approach(color)` | move | finds the colour (ahead, else with a sweep) and runs `go` toward it, up to 3 legs, until ~0.45 m away (`arrived`, `not_found` with a survey, `blocked`, …) |
| `stop` | override | `stopMove()` on walking and postures right away; the robot stays awake |
| `rest` | override | stop, then `rest()` (limp and crouched), always allowed |

- *read* runs any time.
- *move* takes the single movement slot. A second move is refused with "busy".
- *override* always runs and interrupts the current move.

Every movement returns `result` (`completed`, `stopped`, `rested`, `fell`, `obstacle`, `bumped`,
`timeout`, `did_not_move`, `failed`) plus the resulting `state`, so the planner sees what actually happened.

Every successful result also carries one image taken after the command finished (`"images"`): the
top (ahead) camera view above the bottom (feet) one; `look_around` and `scan` return one panorama
instead. One image per result matters: some clients (Antigravity) make the model open every image
with a separate tool call. The MCP server turns it into MCP image content, so the model sees
the picture and the JSON keeps only the metadata (`images_meta`). Yellow lines on each image mark
where the ground is 0.25–3 m ahead of the camera, computed from the camera's height and pitch
(`ALMotion.getPosition("CameraTop", FRAME_ROBOT)`). They're skipped when the robot isn't upright.
A QVGA frame costs the model about 100 tokens. `BRIDGE_AUTO_IMAGE=0` limits images to
`look`/`look_around`/`scan`, and `BRIDGE_IMAGE_GRID=0` drops the lines. naoqisim only fills QVGA buffers, so the
bridge always asks for QVGA, on a real NAO too.

### Adding a primitive

1. Add an entry to `src/primitives.json` with these fields:
   - `name`
   - `kind` (`read` / `move` / `override`)
   - `description` (written for the LLM: units, limits, when to use it)
   - `params`, a JSON Schema object. The supported subset is `type` (number/integer/string/boolean),
     `required`, `enum` and `additionalProperties: false`.
2. Add one handler in `src/bridge.py`:
   ```python
   @primitive("look_at")
   def look_at(call):              # call.p: NAOqi proxies, call.args: validated arguments
       yaw = max(-1.0, min(1.0, call.args["yaw"]))   # clamp here: never trust the arguments
       ...                          # long motions: proxy.post.X(...) + wait_task() so stop works
       return {"result": "completed", "state": read_state(call.p)}
   ```
   `move` handlers already run inside the movement slot. Raise `Refused("reason")` to refuse
   (HTTP 409). The bridge won't start if the registry and the handlers don't match.
3. `docker compose restart bridge`, then restart the MCP server (`/mcp` → reconnect in Claude Code).
   The MCP side needs no code changes.

Stay within the scope: only movement primitives that are safe for an LLM to call, and read-only sensors. No joint
angles (except clamped head yaw/pitch in `look_around`), stiffness, Cartesian control, keyframes, and nothing that touches the protections.

## Safety

Enforced in `src/bridge.py` (and `src/common.py` for scripts). The MCP layer only forwards commands.

The bridge does the seeing for `scan`, `go` and `approach` (`src/perception.py`, flat-floor camera
geometry, no world knowledge): it learns the floor's colour from the ground at the feet and measures,
per image column, how far that floor runs. Measured against Webots ground truth in
`corridor_complex.wbt` this is within ~0.1 m, always on the short side. The model decides where to
go; the bridge walks and stops. Details that keep it safe:

- The bottom camera only counts while the head faces ahead (turned sideways it sees the robot's
  arms), and not its outer 10 % (the hands). Obstacles closer than the top camera can see are
  reported as `upper_bound` ("something closer than this").
- A new floor colour is only learnt if it matches the known one, hardly appears above the horizon
  in the top camera (a plain wall right ahead would), or the sonar has nothing within 0.5 m.
  Unknown floor: `go` returns `blind`.
- In `go`, a sonar echo under `BRIDGE_OBSTACLE_STOP_M` stops the walk unless the cameras (or
  obstacle points remembered from the last 0.6 m of walking) show something at about that
  distance on that side; seen obstacles are left to the camera lane check, which stops closer.

- NAOqi's fall manager, foot-contact protection and collision protections stay as they are. Nothing
  sets them, and no primitive exposes them. (In naoqisim, external collision protection reads as
  off. `/health` and the bridge log show it.)
- Walks stop on their own when a foot bumper is pressed (`bumped`). Forward walks also stop when a
  sonar reads closer than 0.4 m (`obstacle`, `BRIDGE_OBSTACLE_STOP_M`), and don't start then. Backing up,
  sidestepping and turning stay allowed. NAOqi's own external collision stop is off in naoqisim, and the
  sonar misses a lot (Sim limits), so this is a last line of defence and the model has to use the images.
- Joint `fractionMaxSpeed` ≤ 0.3, posture speed ≤ 0.5 (`clamp()`). Walks ≤ 1.0 m and ≤ π rad per call.
- Arguments are checked against `primitives.json`: unknown keys, wrong types, NaN and Infinity are rejected.
- Walking needs an awake robot in the Standing family (not Crouch), finite odometry, and no
  unresolved fall. Otherwise the bridge refuses with the reason.
- Falls (`robotHasFallen`, `ALMotion/RobotIsFalling`, lying posture families) stop the movement.
  Walking stays blocked until `stand` succeeds.
- One movement at a time. `stop` and `rest` always get through.
- Client lost (no request or heartbeat for 20 s, or the caller hangs up mid-movement) or bridge
  shutdown (SIGTERM/SIGINT): the bridge stops and rests (`end_limp`, the same as `safe_session`).
  Cancelling a tool call mid-walk therefore rests the robot.
- Scripts that move run inside `safe_session(motion)`, which ends limp and resting, also on errors and Ctrl-C.
- No `.motion` keyframe replay on the real robot.
- Real robot: the chest button is the e-stop, and a spotter stays within arm's reach.

## Sim limits (NAOQI_ATTEMPT_v1/NOTES.md, plus what the bridge tests showed)

- Walks go ~86 % of the commanded distance, but odometry reports the full distance. Seen here too:
  0.3 m commanded, 0.299 m odometry, 0.25–0.26 m truth. `go` with `moveToward` and step length 0.06
  (`BRIDGE_MAX_STEP_X`; NAOqi's default is 0.04) covered 70–75 % of odometry, at ~0.07–0.1 m/s.
- NaN odometry: naoqisim used to start NAOqi before sending any sensor values, so ALMotion's odometry
  went NaN during the first motion and `moveTo` did nothing (0/10 fresh starts OK). Patch 0004 in
  NAOQI_ATTEMPT_v1 fixes it (10/10). Rebuild naoqisim there (`docker compose build naoqisim`). If
  `get_state` still warns about NaN, `walk_to` refuses. Then stop the bridge and run
  `tools/sim_reset.sh` in NAOQI_ATTEMPT_v1.
- Getting up after a fall fails at the 0.5 speed cap (`stand` times out, the robot ends in e.g. SitRelax).
  Rest, then reset the world.
- `sit` (Sit at 0.5) tips the robot onto its side in Webots (roll −25°, posture family `Left`),
  2 of 2 tries. The bridge reports it as a fall.
- NAOqi counts Crouch as posture family Standing. The bridge refuses to walk from Crouch anyway.
- Restarting naoqisim leaves the robot limp, so it falls. Rest first, and reload the world
  afterwards (Webots, or a `Supervisor().worldReload()` controller for the `truth` robot).
- Camera is QVGA only. ALNavigation, ALLocalization, ALVisionRecognition and ALSystem are missing.
- Sonar only sees surfaces facing it: Webots drops rays hitting at > 22.5°, the two 60° cones point
  ~30° outward, and the range is 0.25–2.55 m. A box straight ahead often reads 2.55 (= no echo).
  Only `US/*/Sensor/Value` is filled (no multi-echo values). Walls are fine:
  `webots/tools/sonar_check.py` (0.24–1.2 m, facing a wall and turned up to 60° from it) read within
  0.02 m on average, 0.11 m at most, with no missed wall closer than 1.5 m.

## Worlds

- `nao_flat.wbt`: flat floor with one box, for testing primitives.
- `corridor.wbt`: 1.3 m corridor with one blind left turn to a green end panel.
- `corridor_complex.wbt`: 1.4 m corridor with five alternating 90° corners to a red end wall.
- `doorway.wbt`: two rooms with a 0.8 m gap in the wall between them, offset from the start.

The last three are ported from WE_NAO_v000. The task and success condition are in each file's header.

Any world works if it keeps these from `nao_flat.wbt`:

- `WorldInfo { basicTimeStep 20 ... }` and the `"NAO foot material"` contact properties
- `DEF NAO Nao { name "NAO" controller "<extern>" }` with the PROTO at its defaults (V5, H25)
- only one NAO, and only one Webots instance open (naoqisim connects to port 1234)
- the `truth` supervisor robot, only if you want `webots/tools/truth.py`

`webots/tools/world_rays.py` ray-casts a world file (true distances around a pose from `truth.py`),
and `webots/tools/sonar_check.py` uses both to check the sonar through the bridge.
