# Shared base for the pilot's harness images: Python 3.12, the MCP server (mcp_server/ + src/primitives.json,
# installed from uv.lock), an unprivileged user and the entrypoint. Built from a staged context that holds only
# those files (pilot/build_images.sh), so no repository, world files or logs end up in any image.
FROM python@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

ARG UV_VERSION=0.12.17
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends ca-certificates curl util-linux zstd >/dev/null \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -q "uv==${UV_VERSION}"

# MCP server: same code and lockfile as the repository, at /opt/naoqi-mcp.
COPY mcp_server/ /opt/naoqi-mcp/
COPY primitives.json /opt/naoqi-mcp/primitives.json
RUN cd /opt/naoqi-mcp && UV_PROJECT_ENVIRONMENT=/opt/naoqi-mcp/.venv uv sync --frozen --no-dev -q \
    && /opt/naoqi-mcp/.venv/bin/python -c "import naoqi_mcp.server" \
    && pip uninstall -y -q uv && rm -rf /root/.cache
ENV NAOQI_PRIMITIVES=/opt/naoqi-mcp/primitives.json \
    NAOQI_BRIDGE_URL=http://bridge:8765

# Fixed identity; the agent runs as an unprivileged user in an empty working directory.
RUN echo 0123456789abcdef0123456789abcdef > /etc/machine-id \
    && useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /work /out && chown agent:agent /work /out
COPY pilot-entrypoint /usr/local/bin/pilot-entrypoint
ENTRYPOINT ["/usr/local/bin/pilot-entrypoint"]
