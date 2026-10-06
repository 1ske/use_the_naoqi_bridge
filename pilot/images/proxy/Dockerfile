# Allowlisting egress proxy, one per run. It joins the run's internal network and a normal network, and is the
# agent container's only way out. PROXY_ALLOW: space-separated anchored host regexes (CONNECT to port 443 only).
FROM alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
RUN apk add --no-cache -q tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY proxy-entrypoint /usr/local/bin/proxy-entrypoint
ENTRYPOINT ["/usr/local/bin/proxy-entrypoint"]
