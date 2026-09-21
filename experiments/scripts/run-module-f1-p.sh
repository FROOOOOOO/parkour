#!/usr/bin/env bash

# Dedicated safe entry point for module-F F1-P.
# The unified runner defaults to dry-run; a real experiment still requires
# the caller to pass --execute explicitly.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run-module-f.sh" --experiment F1-P "$@"
