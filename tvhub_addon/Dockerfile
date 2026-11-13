# The add-on is the normal TVHub image plus one switch: sign-in is delegated to Home Assistant's Ingress.
# TVHUB_AUTH=ingress only takes effect when the Supervisor injects SUPERVISOR_TOKEN and the request comes from it.
ARG BUILD_VERSION
FROM ghcr.io/xavilitoh/tvhub:${BUILD_VERSION}
ENV TVHUB_AUTH=ingress
