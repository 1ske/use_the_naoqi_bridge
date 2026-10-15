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
# Verification builds (instruction and image check): each harness image plus pilot/verify/naoqi_mcp_verify.py.
for name in claude opencode agy; do
    mkdir -p "$STAGE/$name-verify"
    cp "$REPO/pilot/images/verify/Dockerfile" "$REPO/pilot/verify/naoqi_mcp_verify.py" "$STAGE/$name-verify/"
done
harness_ref() { local id; id=$(docker image inspect --format '{{.Id}}' "naoqi-pilot/$1"); docker tag "$id" "naoqi-pilot/$1:${id#sha256:}"; echo "naoqi-pilot/$1:${id#sha256:}"; }
claude_verify_id=$(build claude-verify "$STAGE/claude-verify" --build-arg "HARNESS_IMAGE=$(harness_ref claude)")
opencode_verify_id=$(build opencode-verify "$STAGE/opencode-verify" --build-arg "HARNESS_IMAGE=$(harness_ref opencode)")
agy_verify_id=$(build agy-verify "$STAGE/agy-verify" --build-arg "HARNESS_IMAGE=$(harness_ref agy)")

# Harness versions as the built images report them (each Dockerfile also checks its pinned version).
claude_version=$(docker run --rm --entrypoint claude naoqi-pilot/claude --version | sed 's/ (Claude Code)$//')
opencode_version=$(docker run --rm --entrypoint opencode naoqi-pilot/opencode --version | sed 's/^opencode v//')
agy_version=$(docker run --rm --entrypoint agy naoqi-pilot/agy --version)

commit=$(git -C "$REPO" rev-parse HEAD)
dirty=$(git -C "$REPO" status --porcelain -- mcp_server src/primitives.json pilot/images pilot/verify | wc -l | tr -d ' ')
python3 - "$LOCK" "$commit" "$dirty" "$base_id" "$claude_id" "$opencode_id" "$agy_id" "$proxy_id" \
    "$claude_verify_id" "$opencode_verify_id" "$agy_verify_id" "$claude_version" "$opencode_version" "$agy_version" <<'EOF'
import json, sys, datetime
(lock, commit, dirty, base, claude, opencode, agy, proxy, claude_v, opencode_v, agy_v,
 claude_version, opencode_version, agy_version) = sys.argv[1:]
json.dump({
    "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "built_from_commit": commit,
    "uncommitted_changes_in_image_sources": int(dirty) > 0,
    "images": {"naoqi-pilot/base": base, "naoqi-pilot/claude": claude, "naoqi-pilot/opencode": opencode,
               "naoqi-pilot/agy": agy, "naoqi-pilot/proxy": proxy, "naoqi-pilot/claude-verify": claude_v,
               "naoqi-pilot/opencode-verify": opencode_v, "naoqi-pilot/agy-verify": agy_v},
    "harness_versions": {"claude": claude_version, "opencode": opencode_version, "agy": agy_version},
    "harness_versions_source": "--version of each built image",
}, open(lock, "w"), indent=2)
print(open(lock).read())
EOF
