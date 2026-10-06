#!/bin/bash
# Free, offline check of what each harness image would send to its LLM (not used in runs).
# Runs the image's own pilot-agent on an internal network whose only other host is the fake LLM endpoint
# (capture_llm.py). Credentials are fake and the endpoint answers every request with an error.
#   capture_check.sh claude|opencode|agy OUTDIR
set -euo pipefail
HARNESS=$1 OUT=$2
HERE=$(cd "$(dirname "$0")" && pwd)
NET=pilot-capture
PROMPT="You control a robot at the start of a corridor. Follow the corridor to its end, which is marked by a green panel, and stop in front of the panel. When the robot is there, call rest."
mkdir -p "$OUT/cap" "$OUT/out"
docker network inspect $NET >/dev/null 2>&1 || docker network create --internal $NET >/dev/null
docker rm -f fake-llm >/dev/null 2>&1 || true
docker run -d --name fake-llm --network $NET --entrypoint python \
    -v "$HERE/capture_llm.py":/capture_llm.py:ro -v "$OUT/cap":/cap naoqi-pilot/base /capture_llm.py >/dev/null
sleep 2

common=(--network $NET --hostname agent -v "$OUT/out":/out -e NO_PROXY=bridge,localhost,127.0.0.1,fake-llm)
case $HARNESS in
claude)
    cid=$(docker create "${common[@]}" -e CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-capture-check-not-a-token \
          -e ANTHROPIC_BASE_URL=http://fake-llm:8080 naoqi-pilot/claude pilot-agent "$PROMPT") ;;
opencode)
    cid=$(docker create "${common[@]}" -e OPENROUTER_API_KEY=sk-or-capture-check-not-a-key \
          -e OPENCODE_CONFIG_CONTENT='{"providers":{"openrouter":{"settings":{"baseURL":"http://fake-llm:8080/api/v1"}}}}' \
          naoqi-pilot/opencode pilot-agent "$PROMPT" "${3:-anthropic/claude-opus-5.5}") ;;
agy)
    # Same settings as the image plus modelProvider=gemini, the only way to point agy at another endpoint.
    stage=$(mktemp -d); mkdir -p "$stage/.gemini/antigravity-cli"
    docker run --rm --entrypoint cat naoqi-pilot/agy /home/agent/.gemini/antigravity-cli/settings.json \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); d["modelProvider"]="gemini"; print(json.dumps(d))' \
        > "$stage/.gemini/antigravity-cli/settings.json"
    cid=$(docker create "${common[@]}" -e GEMINI_API_KEY=capture-check-not-a-key \
          -e GOOGLE_GEMINI_BASE_URL=http://fake-llm:8080 naoqi-pilot/agy pilot-agent "$PROMPT")
    docker cp "$stage/." "$cid":/stage >/dev/null; rm -rf "$stage" ;;
esac
( sleep 180; docker stop -t 2 "$cid" >/dev/null 2>&1 ) & watchdog=$!
docker start -a "$cid" || true
kill $watchdog 2>/dev/null || true
echo "harness exit code: $(docker inspect -f '{{.State.ExitCode}}' "$cid")"
docker rm -f "$cid" fake-llm >/dev/null
echo "captured requests: $(wc -l < "$OUT/cap/requests.jsonl" 2>/dev/null || echo 0)"
