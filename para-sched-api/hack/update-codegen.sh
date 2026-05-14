#!/bin/bash
# hack/update-codegen.sh - regenerate all client code

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_ROOT=$(dirname "${BASH_SOURCE[0]}")/..
cd "${SCRIPT_ROOT}"

# Load version configuration
source hack/version.sh

# Read module name from go.mod
MODULE=$(go list -m)
echo "Module: ${MODULE}"

APIS_PKG="${MODULE}/apis"
OUTPUT_PKG="${MODULE}/generated"

echo ""
echo "============================================"
echo "Code Generation for Kubernetes ${KUBE_VERSION}"
echo "  - code-generator: ${CODEGEN_VERSION}"
echo "  - controller-gen: ${CONTROLLER_GEN_VERSION}"
echo "============================================"
echo ""

# Create temp output directory
TEMP_OUTPUT=$(mktemp -d)
trap "rm -rf ${TEMP_OUTPUT}" EXIT

echo ""
echo "============================================"
echo "Code Generation"
echo "  Module: ${MODULE}"
echo "  Temp dir: ${TEMP_OUTPUT}"
echo "============================================"
echo ""

# ============================================
# 1. Generate DeepCopy functions
# ============================================
echo "==> [1/4] Generating deepcopy functions..."
go run sigs.k8s.io/controller-tools/cmd/controller-gen@${CONTROLLER_GEN_VERSION} \
    object:headerFile="hack/boilerplate.go.txt" \
    paths="./apis/..."

# ============================================
# 2. Generate CRD manifests
# ============================================
echo "==> [2/4] Generating CRD manifests..."
# Clean stale YAML files first — controller-gen only writes current CRDs, so if a
# type's plural/path marker changes (e.g. adoptionstatses -> adoptionstats), the
# old YAML stays behind and `kubectl apply -f config/crd/bases/` would register
# BOTH CRDs. The duplicate registration fails with NotAccepted (Kind collision),
# leaving an orphan CRD that controller-manager's GC keeps trying to watch.
rm -rf config/crd/bases
mkdir -p config/crd/bases
go run sigs.k8s.io/controller-tools/cmd/controller-gen@${CONTROLLER_GEN_VERSION} \
    crd:crdVersions=v1 \
    paths="./apis/..." \
    output:crd:artifacts:config=config/crd/bases

# ============================================
# 3. Clean up old generated code
# ============================================
echo "==> [3/4] Cleaning old generated code..."
rm -rf generated/

# ============================================
# 4. Generate clientset, listers, informers
# ============================================
echo "==> [4/4] Generating clientset, listers, informers..."

# Select generation flags based on code-generator version.
# v0.28+ uses the new argument format.
MAJOR_VERSION=$(echo ${CODEGEN_VERSION} | sed 's/v0\.\([0-9]*\).*/\1/')

if [ "${MAJOR_VERSION}" -ge 30 ]; then
    # Kubernetes 1.30+: --output-dir / --output-pkg pair
    # (migration started in v0.29; old --output-package/--output-base removed in v0.30)
    echo "    Using new-style code-generator (v0.30+)..."

    mkdir -p generated/clientset generated/listers generated/informers

    # client-gen: clientset-name=versioned -> output to generated/clientset/versioned/...
    go run k8s.io/code-generator/cmd/client-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --clientset-name="versioned" \
        --input-base="" \
        --input="${APIS_PKG}/v1" \
        --output-dir="generated/clientset" \
        --output-pkg="${OUTPUT_PKG}/clientset"

    # lister-gen
    go run k8s.io/code-generator/cmd/lister-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --output-dir="generated/listers" \
        --output-pkg="${OUTPUT_PKG}/listers" \
        "${APIS_PKG}/v1"

    # informer-gen
    go run k8s.io/code-generator/cmd/informer-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --versioned-clientset-package="${OUTPUT_PKG}/clientset/versioned" \
        --listers-package="${OUTPUT_PKG}/listers" \
        --output-dir="generated/informers" \
        --output-pkg="${OUTPUT_PKG}/informers" \
        "${APIS_PKG}/v1"

    # New-style generators write directly to the target path; skip TEMP_OUTPUT merge.
    SKIP_TEMP_COPY=1

elif [ "${MAJOR_VERSION}" -ge 28 ]; then
    # Kubernetes 1.28-1.29 transition: client-gen still accepts old --output-package/--output-base;
    # lister-gen/informer-gen have already switched to --output-pkg. Mix both styles.
    echo "    Using transitional code-generator (v0.28-v0.29)..."

    go run k8s.io/code-generator/cmd/client-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --input-base="" \
        --input="${APIS_PKG}/v1" \
        --output-package="${OUTPUT_PKG}/clientset" \
        --output-base="${TEMP_OUTPUT}" \
        --clientset-name="versioned"

    go run k8s.io/code-generator/cmd/lister-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --output-base="${TEMP_OUTPUT}" \
        --output-pkg="${OUTPUT_PKG}/listers" \
        "${APIS_PKG}/v1"

    go run k8s.io/code-generator/cmd/informer-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --output-package="${OUTPUT_PKG}/informers" \
        --output-base="${TEMP_OUTPUT}" \
        --versioned-clientset-package="${OUTPUT_PKG}/clientset/versioned" \
        --listers-package="${OUTPUT_PKG}/listers" \
        "${APIS_PKG}/v1"

else
    # Kubernetes 1.27 and earlier: old-style flags
    echo "    Using old-style code-generator (v0.27 and earlier)..."

    # client-gen
    go run k8s.io/code-generator/cmd/client-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --input-base="" \
        --input="${APIS_PKG}/v1" \
        --output-package="${OUTPUT_PKG}/clientset" \
        --output-base="${TEMP_OUTPUT}" \
        --clientset-name="versioned"

    # lister-gen
    go run k8s.io/code-generator/cmd/lister-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --input-dirs="${APIS_PKG}/v1" \
        --output-package="${OUTPUT_PKG}/listers" \
        --output-base="${TEMP_OUTPUT}"

    # informer-gen
    go run k8s.io/code-generator/cmd/informer-gen@${CODEGEN_VERSION} \
        --go-header-file="hack/boilerplate.go.txt" \
        --input-dirs="${APIS_PKG}/v1" \
        --versioned-clientset-package="${OUTPUT_PKG}/clientset/versioned" \
        --listers-package="${OUTPUT_PKG}/listers" \
        --output-package="${OUTPUT_PKG}/informers" \
        --output-base="${TEMP_OUTPUT}"
fi

# ============================================
# Move to final location (old-style/transitional generators route through TEMP_OUTPUT;
# new-style v0.30+ write directly to generated/ and skip this step)
# ============================================
if [ "${SKIP_TEMP_COPY:-0}" != "1" ]; then
    echo "==> Moving generated files from temp dir..."
    mkdir -p generated/
    if [ -d "${TEMP_OUTPUT}/${MODULE}/generated" ]; then
        cp -r "${TEMP_OUTPUT}/${MODULE}/generated/"* generated/
    fi
fi

echo ""
echo "============================================"
echo "Code generation complete!"
echo "============================================"
echo ""
echo "Generated files:"
echo "  ✓ apis/v1/zz_generated.deepcopy.go"
echo "  ✓ config/crd/bases/*.yaml"
echo "  ✓ generated/clientset/"
echo "  ✓ generated/listers/"
echo "  ✓ generated/informers/"
echo ""
echo "Next steps:"
echo "  1. Review generated code"
echo "  2. Run 'go mod tidy'"
echo "  3. Run 'go build ./...'"
