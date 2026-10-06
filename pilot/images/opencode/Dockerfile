# A1-A4: OpenCode 2.0.21 (npm @opencode/cli-linux-arm64, checked against the registry's sha512).
ARG BASE
FROM ${BASE}
ARG OPENCODE_VERSION=2.0.21
ARG OPENCODE_SHA512=d8ff0087801ef53ca54ab372e81a3dc7d329b1a7ef9b294cd072788c7b18d26e01b1d5e964ec35afc27aabc393a6ebace8250eb936008166673f11103bd19a23
RUN curl -fsSL -o /tmp/oc.tgz "https://registry.npmjs.org/@opencode/cli-linux-arm64/-/cli-linux-arm64-${OPENCODE_VERSION}.tgz" \
    && echo "${OPENCODE_SHA512}  /tmp/oc.tgz" | sha512sum -c - \
    && tar -xzf /tmp/oc.tgz -C /tmp package/bin/opencode \
    && install -o root -g root -m 755 /tmp/package/bin/opencode /usr/local/bin/opencode \
    && rm -rf /tmp/oc.tgz /tmp/package \
    && opencode --version | grep -qx "opencode v${OPENCODE_VERSION}"
COPY opencode.json /home/agent/.config/opencode/opencode.json
COPY pilot-agent /usr/local/bin/pilot-agent
RUN chown -R agent:agent /home/agent/.config
LABEL pilot.harness=opencode pilot.harness_version=${OPENCODE_VERSION}
