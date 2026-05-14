#!/bin/bash

# Install KWOK sharded systemd service files
# Each shard manages nodes with annotation kwok.x-k8s.io/node=fakeN
#
# Usage: sudo ./install-kwok-services.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_DIR="$SCRIPT_DIR/systemd"

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)"
    exit 1
fi

echo "Installing KWOK sharded service files..."
echo ""

for f in "$SERVICE_DIR"/kwok*.service; do
    name=$(basename "$f")
    cp "$f" "$SYSTEMD_DIR/$name"
    echo "  Installed $name"
done

systemctl daemon-reload
echo ""
echo "Service files installed. To start all shards:"
echo "  sudo systemctl enable --now kwok kwok{1..9}"
echo ""
echo "To check status:"
echo "  systemctl status kwok kwok{1..9}"
echo ""
echo "Annotation mapping:"
echo "  kwok.service  -> kwok.x-k8s.io/node=fake0"
echo "  kwok1.service -> kwok.x-k8s.io/node=fake1"
echo "  ...           -> ..."
echo "  kwok9.service -> kwok.x-k8s.io/node=fake9"
