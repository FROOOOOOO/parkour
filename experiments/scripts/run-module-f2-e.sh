#!/usr/bin/env bash

# Dedicated safe entry point for module-F F2-E.
# It defaults to dry-run.  Use --injection-smoke-test to validate the 1%
# Dreal-F1 failure profile, or --execute to run the complete E2/E3 x 3-trial matrix.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run-module-f.sh" --experiment F2-E "$@"
