#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"

echo "Clearing stale Chromium lock files..."
docker run --rm -v pepper_whatsapp_session:/data alpine \
    sh -c "rm -f /data/session/SingletonLock /data/session/SingletonSocket /data/session/SingletonCookie"

echo "Restarting whatsapp-bridge..."
docker-compose restart whatsapp-bridge

echo "Waiting for bridge to start..."
sleep 5

echo "Tailing logs (Ctrl-C when done scanning QR code)..."
docker-compose logs -f whatsapp-bridge
