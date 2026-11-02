#!/bin/bash
# Build and start TVHub in Docker, then open it in your browser.
cd "$(dirname "$0")" || exit 1
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Get Docker Desktop: https://www.docker.com/products/docker-desktop/"
  exit 1
fi
docker compose up -d --build || exit 1
echo
echo "TVHub is running → http://localhost:8080"
echo "Logs:  docker compose logs -f      Stop:  docker compose down"
sleep 2
open "http://localhost:8080" 2>/dev/null || true
