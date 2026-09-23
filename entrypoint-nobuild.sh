#!/bin/bash
# Arranque sin "docker build" (útil si Portainer no encuentra el Dockerfile).
set -e
MARK=/var/lib/media-portal/ffmpeg-ready
if [ ! -f "$MARK" ]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg
  mkdir -p /var/lib/media-portal
  touch "$MARK"
fi
exec python3 -u /app/app.py
