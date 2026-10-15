#!/bin/bash
# Free check that a harness image reaches its real LLM endpoint only through the allowlisting proxy (not used
# in runs). Fake credentials, so the endpoint rejects the first request and no model runs.
#   proxy_check.sh claude|opencode OUTDIR
set -euo pipefail
HARNESS=$1 OUT=$2
NET=pilot-proxycheck PROXY=pilot-proxycheck-proxy
mkdir -p "$OUT/out"
case $HARNESS in
claude)   allow='^api\.anthropic\.com$'
          env=(-e CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-proxy-check-not-a-token)
          cmd=(naoqi-pilot/claude pilot-agent "Say hello." claude-opus-5-5) ;;
opencode) allow='^openrouter\.ai$'
          env=(-e OPENROUTER_API_KEY=sk-or-v1-proxy-check-not-a-key)
          cmd=(naoqi-pilot/opencode pilot-agent "Say hello." anthropic/claude-opus-5.5) ;;
esac
docker network inspect $NET >/dev/null 2>&1 || docker network create --internal $NET >/dev/null
docker rm -f $PROXY >/dev/null 2>&1 || true
docker run -d --name $PROXY -e PROXY_ALLOW="$allow" naoqi-pilot/proxy >/dev/null
docker network connect $NET $PROXY
sleep 2
P=http://$PROXY:8888
docker run --rm --network $NET --hostname agent -v "$OUT/out":/out "${env[@]}" \
    -e HTTPS_PROXY=$P -e HTTP_PROXY=$P -e https_proxy=$P -e http_proxy=$P \
    -e NO_PROXY=bridge,localhost,127.0.0.1 -e no_proxy=bridge,localhost,127.0.0.1 "${cmd[@]}" || true
docker logs $PROXY 2>&1 | grep -o -E 'CONNECT [^ ]+|filtered domain "[^"]+"' | sort | uniq -c > "$OUT/proxy-hosts.txt"
docker rm -f $PROXY >/dev/null; docker network rm $NET >/dev/null
cat "$OUT/proxy-hosts.txt"
