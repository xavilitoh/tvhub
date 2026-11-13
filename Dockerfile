FROM python:3.12-slim-bookworm

# adb comes from Debian's android-tools. Inside a Linux container it is not subject to
# macOS "Local Network" privacy, which blocks the native adb binary on a Mac.
RUN apt-get update \
 && apt-get install -y --no-install-recommends adb ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# HOME=/data so ~/.android/adbkey (the key the TV authorises) survives container rebuilds.
ENV HOME=/data DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Bundled for the Home Assistant add-on's optional "install the integration" (see app/addon.py)
COPY custom_components/tvhub /opt/tvhub-integration

EXPOSE 8080
CMD ["python", "app/server.py"]
