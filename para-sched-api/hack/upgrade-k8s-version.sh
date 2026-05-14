#!/bin/bash
# hack/upgrade-k8s-version.sh - one-step Kubernetes version upgrade

set -o errexit
set -o nounset
set -o pipefail

# Target version (override via first argument)
TARGET_VERSION="${1:-1.24}"

SCRIPT_ROOT=$(dirname "${BASH_SOURCE[0]}")/..
cd "${SCRIPT_ROOT}"

echo "============================================"
echo "Upgrading to Kubernetes ${TARGET_VERSION}"
echo "============================================"

# 1. Patch version configuration
echo ""
echo "==> Step 1: Updating version configuration..."
sed -i "s/KUBE_VERSION=\".*\"/KUBE_VERSION=\"${TARGET_VERSION}\"/" hack/version.sh

# 2. Verify Go version requirement
source hack/version.sh
CURRENT_GO=$(go version | grep -oP 'go\K[0-9]+\.[0-9]+')
echo ""
echo "==> Step 2: Checking Go version..."
echo "    Required: Go ${GO_VERSION}+"
echo "    Current:  Go ${CURRENT_GO}"

# 3. Update module dependencies
echo ""
echo "==> Step 3: Updating dependencies..."
./hack/update-deps.sh

# 4. Regenerate code
echo ""
echo "==> Step 4: Regenerating code..."
./hack/update-codegen.sh

# 5. Verify build
echo ""
echo "==> Step 5: Verifying build..."
go build ./...

echo ""
echo "============================================"
echo "Upgrade complete!"
echo "============================================"
echo ""
echo "Please test thoroughly before committing."