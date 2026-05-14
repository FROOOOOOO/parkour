#!/bin/bash
# Build Docker images for para-sched components on the lab cluster.
#
# All three images use local Go compilation + scratch base image,
# avoiding the need to pull any remote base images (golang, distroless, etc).
#
# Builds:
#   1. para-scheduler/binder:latest
#   2. para-scheduler/dispatcher:latest
#   3. para-scheduler/kube-scheduler:latest
#
# Usage:
#   ./build-images.sh                  # build all
#   ./build-images.sh --binder         # build binder only
#   ./build-images.sh --dispatcher     # build dispatcher only
#   ./build-images.sh --scheduler      # build kube-scheduler only
#   ./build-images.sh --tag v0.1       # custom tag (default: latest)
#
# Prerequisites: docker, go (1.25+)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TAG="${TAG:-latest}"
BUILD_BINDER=false
BUILD_DISPATCHER=false
BUILD_SCHEDULER=false
BUILD_ALL=true

while [[ $# -gt 0 ]]; do
    case $1 in
        --binder)     BUILD_BINDER=true; BUILD_ALL=false; shift ;;
        --dispatcher) BUILD_DISPATCHER=true; BUILD_ALL=false; shift ;;
        --scheduler)  BUILD_SCHEDULER=true; BUILD_ALL=false; shift ;;
        --tag)        TAG="$2"; shift 2 ;;
        -h|--help)    head -20 "$0" | tail -18; exit 0 ;;
        *)            echo "Unknown option: $1"; exit 1 ;;
    esac
done

if $BUILD_ALL; then
    BUILD_BINDER=true
    BUILD_DISPATCHER=true
    BUILD_SCHEDULER=true
fi

log() { echo ">>> [$(date +%H:%M:%S)] $*"; }

cd "$PROJECT_ROOT"

# Shared scratch Dockerfile template.
# Usage: echo "$SCRATCH_DOCKERFILE" | docker build -t <tag> -f - <context-dir>
SCRATCH_DOCKERFILE='FROM scratch
COPY %BINARY% /%BINARY%
USER 65534:65534
ENTRYPOINT ["/%BINARY%"]
'

build_scratch_image() {
    local name="$1"    # binary name (e.g. binder)
    local bin_path="$2" # path to compiled binary
    local tag="$3"

    local tmpdir
    tmpdir=$(mktemp -d)
    cp "$bin_path" "$tmpdir/$name"

    cat > "$tmpdir/Dockerfile" <<EOF
FROM scratch
COPY $name /$name
USER 65534:65534
ENTRYPOINT ["/$name"]
EOF

    docker build -t "para-scheduler/${name}:${tag}" "$tmpdir"
    rm -rf "$tmpdir"
}

# ---------- Binder ----------
if $BUILD_BINDER; then
    log "Compiling binder..."
    cd para-scheduler
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -mod=vendor -o _output/bin/binder ./cmd/binder/
    cd "$PROJECT_ROOT"

    log "Building binder image (para-scheduler/binder:${TAG})..."
    build_scratch_image binder para-scheduler/_output/bin/binder "$TAG"
    log "Binder image done."
fi

# ---------- Dispatcher ----------
if $BUILD_DISPATCHER; then
    log "Compiling dispatcher..."
    cd para-scheduler
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -mod=vendor -o _output/bin/dispatcher ./cmd/dispatcher/
    cd "$PROJECT_ROOT"

    log "Building dispatcher image (para-scheduler/dispatcher:${TAG})..."
    build_scratch_image dispatcher para-scheduler/_output/bin/dispatcher "$TAG"
    log "Dispatcher image done."
fi

# ---------- kube-scheduler ----------
if $BUILD_SCHEDULER; then
    log "Compiling kube-scheduler..."
    cd k8s-scheduler
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build -mod=vendor -o _output/bin/kube-scheduler \
        ./cmd/kube-scheduler/
    cd "$PROJECT_ROOT"

    log "Building kube-scheduler image (para-scheduler/kube-scheduler:${TAG})..."
    build_scratch_image kube-scheduler k8s-scheduler/_output/bin/kube-scheduler "$TAG"
    log "kube-scheduler image done."
fi

# ---------- Summary ----------
echo ""
echo "============================================"
echo "  Build complete (tag: ${TAG})"
echo "============================================"
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}' \
    | grep -E 'REPOSITORY|para-scheduler' || true
