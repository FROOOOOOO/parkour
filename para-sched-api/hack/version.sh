# hack/version.sh - version configuration

# ============================================
# Target Kubernetes version (edit here)
# ============================================
KUBE_VERSION="1.33"

# ============================================
# Derived tool versions
#
# code-generator tracks the K8s minor version (latest patch for that minor).
# controller-gen is independent of K8s and follows Go version:
#   - v0.20.1 (2025-02) requires k8s.io/* v0.35, supports Go 1.23/1.24/1.25
#   - Use v0.20.1 for all K8s >= 1.31 (CRD v1 schema is backwards-compatible)
# Go version is the minimum official build version for that K8s minor.
# ============================================
case "${KUBE_VERSION}" in
    "1.24")
        CODEGEN_VERSION="v0.24.6"
        CONTROLLER_GEN_VERSION="v0.20.0"
        GO_VERSION="1.19"
        ;;
    "1.25")
        CODEGEN_VERSION="v0.25.16"
        CONTROLLER_GEN_VERSION="v0.20.0"
        GO_VERSION="1.19"
        ;;
    "1.26")
        CODEGEN_VERSION="v0.26.15"
        CONTROLLER_GEN_VERSION="v0.20.0"
        GO_VERSION="1.20"
        ;;
    "1.27")
        CODEGEN_VERSION="v0.27.16"
        CONTROLLER_GEN_VERSION="v0.20.0"
        GO_VERSION="1.20"
        ;;
    "1.28")
        CODEGEN_VERSION="v0.28.12"
        CONTROLLER_GEN_VERSION="v0.20.0"
        GO_VERSION="1.21"
        ;;
    "1.29")
        CODEGEN_VERSION="v0.29.8"
        CONTROLLER_GEN_VERSION="v0.20.0"
        GO_VERSION="1.21"
        ;;
    "1.30")
        CODEGEN_VERSION="v0.30.4"
        CONTROLLER_GEN_VERSION="v0.20.0"
        GO_VERSION="1.22"
        ;;
    "1.31")
        CODEGEN_VERSION="v0.31.1"
        CONTROLLER_GEN_VERSION="v0.20.1"
        GO_VERSION="1.22"
        ;;
    "1.32")
        CODEGEN_VERSION="v0.32.13"
        CONTROLLER_GEN_VERSION="v0.20.1"
        GO_VERSION="1.23"
        ;;
    "1.33")
        CODEGEN_VERSION="v0.33.4"
        CONTROLLER_GEN_VERSION="v0.20.1"
        GO_VERSION="1.24"
        ;;
    "1.34")
        CODEGEN_VERSION="v0.34.7"
        CONTROLLER_GEN_VERSION="v0.20.1"
        GO_VERSION="1.24"
        ;;
    *)
        echo "Unsupported Kubernetes version: ${KUBE_VERSION}"
        exit 1
        ;;
esac

echo "Target Kubernetes: ${KUBE_VERSION}"
echo "  - code-generator: ${CODEGEN_VERSION}"
echo "  - controller-gen: ${CONTROLLER_GEN_VERSION}"
echo "  - Go version: ${GO_VERSION}+"