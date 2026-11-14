# Test build: the image CI publishes from every push to the dev branch (ghcr.io/wxberroa/tvhub:dev).
# Own Ingress port so it can run next to the stable add-on (both use the host network).
ARG BUILD_VERSION
FROM ghcr.io/wxberroa/tvhub:${BUILD_VERSION}
ENV TVHUB_AUTH=ingress TVHUB_INGRESS_PORT=8098 TVHUB_CHANNEL=dev
