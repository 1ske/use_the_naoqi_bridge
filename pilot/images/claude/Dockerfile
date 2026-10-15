# A1, A3, A5: Claude Code 2.1.295 (native linux-arm64 build, checked against the release manifest's sha256).
# 2.1.295, the version on the author's Mac on 2026-10-15; 2.1.280 priced Claude Haiku 5.5 at Sonnet's rate (decision 2026-10-15).
ARG BASE
FROM ${BASE}
ARG CLAUDE_VERSION=2.1.295
ARG CLAUDE_SHA256=cfb9dc1332fb6f92a683f835823e91b3179a803398ca7852cd2381a3cb1dea3b
RUN curl -fsSL -o /tmp/claude "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-arm64/claude" \
    && echo "${CLAUDE_SHA256}  /tmp/claude" | sha256sum -c - \
    && install -o root -g root -m 755 /tmp/claude /usr/local/bin/claude && rm /tmp/claude \
    && test "$(claude --version)" = "${CLAUDE_VERSION} (Claude Code)"
COPY mcp.json claude-settings.json /etc/pilot/
COPY pilot-agent /usr/local/bin/pilot-agent
ENV DISABLE_AUTOUPDATER=1
LABEL pilot.harness=claude-code pilot.harness_version=${CLAUDE_VERSION}
