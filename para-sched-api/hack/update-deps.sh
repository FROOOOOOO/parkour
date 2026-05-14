#!/bin/bash
# hack/update-deps.sh - update Go module dependencies

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_ROOT=$(dirname "${BASH_SOURCE[0]}")/..
cd "${SCRIPT_ROOT}"

# Load version configuration
source hack/version.sh

echo ""
echo "==> Updating go.mod for Kubernetes ${KUBE_VERSION}..."

# Update core Kubernetes dependencies
go get k8s.io/api@${CODEGEN_VERSION}
go get k8s.io/apimachinery@${CODEGEN_VERSION}
go get k8s.io/client-go@${CODEGEN_VERSION}
go get k8s.io/code-generator@${CODEGEN_VERSION}

# Update controller-runtime if in use:
# go get sigs.k8s.io/controller-runtime@v0.16.0

echo "==> Running go mod tidy..."
go mod tidy

echo ""
echo "==> Dependencies updated!"
echo "==> Please verify go.mod and run './hack/update-codegen.sh'"