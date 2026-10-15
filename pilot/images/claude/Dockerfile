# A1, A3, A5: Claude Code 2.1.280 (native linux-arm64 build, checked against the release manifest's sha256).
ARG BASE
FROM ${BASE}
ARG CLAUDE_VERSION=2.1.280
ARG CLAUDE_SHA256=92f2b4fd05d0bdcf7b9a0d4e0ecef4a1e4b368b290cd8fd07cff9a50013f45a2
RUN curl -fsSL -o /tmp/claude "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-arm64/claude" \
    && echo "${CLAUDE_SHA256}  /tmp/claude" | sha256sum -c - \
    && install -o root -g root -m 755 /tmp/claude /usr/local/bin/claude && rm /tmp/claude \
    && test "$(claude --version)" = "${CLAUDE_VERSION} (Claude Code)"
COPY mcp.json claude-settings.json /etc/pilot/
COPY pilot-agent /usr/local/bin/pilot-agent
ENV DISABLE_AUTOUPDATER=1
LABEL pilot.harness=claude-code pilot.harness_version=${CLAUDE_VERSION}
