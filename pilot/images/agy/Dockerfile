# A7: Antigravity CLI (agy) 1.2.17, pinned by URL and sha512 from the linux_arm64 release manifest.
# 1.2.16, the version on the author's Mac, has no Linux download (decision 2026-10-05).
ARG BASE
FROM ${BASE}
ARG AGY_VERSION=1.2.17
ARG AGY_URL=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.2.17-6683332533157888/linux-arm/cli_linux_arm64.tar.gz
ARG AGY_SHA512=a2316f5b02ed8e354c9229525f7a3239c1e249f33a1d0442867c68a39b2453654e8d0c48c36bf97b2a3fcef4afc07a7f79eea2a8950b5710abb8777d1a3d3756
RUN curl -fsSL -o /tmp/agy.tgz "${AGY_URL}" \
    && echo "${AGY_SHA512}  /tmp/agy.tgz" | sha512sum -c - \
    && tar -xzf /tmp/agy.tgz -C /tmp antigravity \
    && install -o root -g root -m 755 /tmp/antigravity /usr/local/bin/agy \
    && rm /tmp/agy.tgz /tmp/antigravity \
    && test "$(agy --version)" = "${AGY_VERSION}"
COPY settings.json /home/agent/.gemini/antigravity-cli/settings.json
COPY mcp_config.json /home/agent/.gemini/config/mcp_config.json
# Option (b), decided 2026-10-05: custom agent with no built-in tools and agy's own identity text.
COPY agent.md /home/agent/.gemini/antigravity-cli/agents/nao/agent.md
COPY pilot-agent /usr/local/bin/pilot-agent
RUN chown -R agent:agent /home/agent/.gemini
LABEL pilot.harness=agy pilot.harness_version=${AGY_VERSION}
