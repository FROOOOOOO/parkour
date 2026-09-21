#!/usr/bin/env bash

# Dedicated safe entry point for module-F F2-P.
# It defaults to dry-run; --execute runs P1/P4 under Dreal-F1.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run-module-f.sh" --experiment F2-P "$@"
