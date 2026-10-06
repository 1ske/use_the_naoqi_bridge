#!/bin/bash
# Builds the pilot's images from staged, minimal build contexts and records their IDs in pilot/images.lock.json.
#   base:     mcp_server/ (code, pyproject.toml, uv.lock) + src/primitives.json + pilot/images/base/
#   claude, opencode, agy, proxy: only their own pilot/images/<name>/ directory
# Nothing else from the repository (worlds, logs, other code) is in any build context.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
LOCK="$REPO/pilot/images.lock.json"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

stage_base() {
    local ctx="$STAGE/base"
    mkdir -p "$ctx/mcp_server/naoqi_mcp"
    cp "$REPO"/mcp_server/naoqi_mcp/*.py "$ctx/mcp_server/naoqi_mcp/"
    cp "$REPO"/mcp_server/pyproject.toml "$REPO"/mcp_server/uv.lock "$ctx/mcp_server/"
    cp "$REPO"/src/primitives.json "$ctx/primitives.json"
    cp "$REPO"/pilot/images/base/* "$ctx/"
    chmod +x "$ctx/pilot-entrypoint"
}

build() {  # build NAME CONTEXT [build-args...]
    local name=$1 ctx=$2; shift 2
    chmod +x "$ctx"/pilot-agent "$ctx"/proxy-entrypoint 2>/dev/null || true
    docker build -q --platform linux/arm64 -t "naoqi-pilot/$name" "$@" "$ctx" >/dev/null
    docker image inspect --format '{{.Id}}' "naoqi-pilot/$name"
}

stage_base
base_id=$(build base "$STAGE/base")
base_ref="naoqi-pilot/base:${base_id#sha256:}"  # BuildKit resolves FROM by tag, not by bare image ID
docker tag "$base_id" "$base_ref"
for name in claude opencode agy proxy; do cp -r "$REPO/pilot/images/$name" "$STAGE/$name"; done
claude_id=$(build claude "$STAGE/claude" --build-arg "BASE=$base_ref")
opencode_id=$(build opencode "$STAGE/opencode" --build-arg "BASE=$base_ref")
agy_id=$(build agy "$STAGE/agy" --build-arg "BASE=$base_ref")
proxy_id=$(build proxy "$STAGE/proxy")

commit=$(git -C "$REPO" rev-parse HEAD)
dirty=$(git -C "$REPO" status --porcelain -- mcp_server src/primitives.json pilot/images | wc -l | tr -d ' ')
python3 - "$LOCK" "$commit" "$dirty" "$base_id" "$claude_id" "$opencode_id" "$agy_id" "$proxy_id" <<'EOF'
import json, sys, datetime
lock, commit, dirty, base, claude, opencode, agy, proxy = sys.argv[1:]
json.dump({
    "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "built_from_commit": commit,
    "uncommitted_changes_in_image_sources": int(dirty) > 0,
    "images": {"naoqi-pilot/base": base, "naoqi-pilot/claude": claude, "naoqi-pilot/opencode": opencode,
               "naoqi-pilot/agy": agy, "naoqi-pilot/proxy": proxy},
    "harness_versions": {"claude": "2.1.280", "opencode": "2.0.21", "agy": "1.2.17"},
}, open(lock, "w"), indent=2)
print(open(lock).read())
EOF
