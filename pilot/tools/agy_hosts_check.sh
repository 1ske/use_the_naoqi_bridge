#!/bin/bash
# Check of which hosts agy contacts in a session (not used in runs). Runs agy with the template login and the prompt
# "Say OK." behind a proxy that allows every host except the ones A7 (agy) keeps blocked (feature flags, logging) and other
# Gemini endpoints. agy's eligibility check needs its model host (daily-cloudcode-pa, loadCodeAssist), so this makes
# one small model request on the Google login. Prints the hosts agy connected to and the ones it was refused.
#   agy_hosts_check.sh OUTDIR
set -euo pipefail
OUT=$1
NET=pilot-agyhosts PROXY=pilot-agyhosts-proxy AGENT=pilot-agyhosts-agent
TOKEN="$HOME/.naoqi-pilot/templates/agy/antigravity-cli/antigravity-oauth-token"
mkdir -p "$OUT"
cleanup() { docker rm -f $AGENT $PROXY >/dev/null 2>&1 || true; docker network rm $NET >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
conf=$(mktemp -d)
cat > "$conf/tinyproxy.conf" <<'CONF'
Port 8888
Listen 0.0.0.0
Timeout 120
Allow 0.0.0.0/0
LogLevel Info
FilterDefaultDeny No
FilterURLs Off
FilterExtended On
Filter "/etc/tinyproxy/deny"
ConnectPort 443
CONF
printf '%s\n' 'generativelanguage\.googleapis\.com$' 'aiplatform\.googleapis\.com$' \
    '^play\.googleapis\.com$' 'antigravity-unleash\.goog$' > "$conf/deny"
docker network create --internal $NET >/dev/null
docker run -d --name $PROXY -v "$conf/tinyproxy.conf:/etc/tinyproxy/tinyproxy.conf:ro" -v "$conf/deny:/etc/tinyproxy/deny:ro" \
    --entrypoint tinyproxy naoqi-pilot/proxy -d -c /etc/tinyproxy/tinyproxy.conf >/dev/null
docker network connect $NET $PROXY
P=http://$PROXY:8888
docker create --name $AGENT --network $NET --hostname agent -v "$OUT":/out \
    -e HTTPS_PROXY=$P -e HTTP_PROXY=$P -e https_proxy=$P -e http_proxy=$P \
    -e NO_PROXY=bridge,localhost,127.0.0.1 -e no_proxy=bridge,localhost,127.0.0.1 \
    naoqi-pilot/agy pilot-agent "Say OK." gemini-3.8-flash-medium >/dev/null
stage=$(mktemp -d); mkdir -p "$stage/.gemini/antigravity-cli"
cp "$TOKEN" "$stage/.gemini/antigravity-cli/antigravity-oauth-token"; chmod 600 "$stage/.gemini/antigravity-cli/antigravity-oauth-token"
docker cp "$stage/." $AGENT:/stage >/dev/null; rm -rf "$stage"
( sleep 90; docker stop -t 2 $AGENT >/dev/null 2>&1 ) & watchdog=$!
docker start -a $AGENT >/dev/null 2>&1 || true
kill $watchdog 2>/dev/null || true
docker logs $PROXY 2>&1 | grep -o -E 'Established connection to host "[^"]+"|filtered domain "[^"]+"|CONNECT [^ ]+' | sort | uniq -c > "$OUT/hosts.txt"
cat "$OUT/hosts.txt"
echo "--- agy's error:"; tail -c 400 "$OUT/harness-stderr.log"; echo
rm -rf "$conf"
