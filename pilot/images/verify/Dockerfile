# Verification build of a harness image for the instruction and image check (section 5.1.5): the harness image
# unchanged, except that the naoqi-mcp command runs pilot/verify/naoqi_mcp_verify.py (the same server with a random
# word in its instructions and a stand-in bridge whose get_state image shows a random code). Not used in runs.
ARG HARNESS_IMAGE
FROM ${HARNESS_IMAGE}
COPY naoqi_mcp_verify.py /opt/naoqi-mcp/naoqi_mcp_verify.py
RUN printf '#!/bin/sh\nexec /opt/naoqi-mcp/.venv/bin/python /opt/naoqi-mcp/naoqi_mcp_verify.py "$@"\n' \
        > /opt/naoqi-mcp/.venv/bin/naoqi-mcp \
    && chmod 755 /opt/naoqi-mcp/.venv/bin/naoqi-mcp
LABEL pilot.verification_build=1
