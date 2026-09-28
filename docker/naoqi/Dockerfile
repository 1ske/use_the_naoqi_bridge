# Python 2.7 + pynaoqi 2.1.4.13 client, runs the code in /app.
# Based on naoqi_pySketch from https://github.com/i4c0ni99/Docker-virtualNao-PyNaoqi
# (only the pynaoqi part: SDK tarball + the boost 1.55 fix, see
#  https://community.ald.softbankrobotics.com/en/forum/import-issue-pynaoqi-214-ubuntu-7956).
FROM --platform=linux/amd64 python:2.7

ARG PEER_REPO=https://github.com/i4c0ni99/Docker-virtualNao-PyNaoqi
ARG PEER_COMMIT=40bd1df6e17852fb5502cdfb0e4be1c757a70090
ARG PYNAOQI=pynaoqi-python2.7-2.1.4.13-linux64
ARG PYNAOQI_SHA256=1aa6ab186d2effc457ca0bebd7ff5eec500da0c88e91ca8cb90e558e7c5d4f4c

# Last py2.7 wheels; Pillow is for camera.py
RUN pip install --no-cache-dir "numpy<1.17" "Pillow<7"

WORKDIR /naoqi
# The tarball is a Git LFS object in the peer repo, so fetch it from the LFS media URL.
RUN git clone -q ${PEER_REPO} /tmp/peer \
    && git -C /tmp/peer checkout -q ${PEER_COMMIT} \
    && curl -fsSL -o ${PYNAOQI}.tar.gz \
       https://media.githubusercontent.com/media/i4c0ni99/Docker-virtualNao-PyNaoqi/${PEER_COMMIT}/naoqi_pySketch/${PYNAOQI}.tar.gz \
    && echo "${PYNAOQI_SHA256}  ${PYNAOQI}.tar.gz" | sha256sum -c - \
    && tar xzf ${PYNAOQI}.tar.gz && rm ${PYNAOQI}.tar.gz \
    && cp /tmp/peer/naoqi_pySketch/boost/* ${PYNAOQI}/ \
    && rm -rf /tmp/peer

ENV PYTHONPATH=/naoqi/${PYNAOQI}
ENV LD_LIBRARY_PATH=/naoqi/${PYNAOQI}
RUN python -c "import naoqi; print('pynaoqi OK')"

WORKDIR /app
CMD ["bash"]
