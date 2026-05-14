#!/bin/bash
# Setup script for local Kind cluster para-sched environment.
#
# Prerequisites:
#   - kind, kubectl, docker, go installed
#   - gcr.io/distroless/static:nonroot image available locally
#
# Usage:
#   ./setup.sh [NUM_SCHEDULERS]    # full: build + cluster + deploy (default: 3)
#   ./setup.sh --build-only        # only build docker images
#   ./setup.sh --deploy-only [N]   # only deploy (cluster must exist)
#   ./setup.sh --teardown          # delete the cluster

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CLUSTER_NAME="para-sched"
CTX="kind-${CLUSTER_NAME}"

# ---------- helpers ----------

log()  { echo ">>> $*"; }

# ---------- build ----------

do_build() {
    log "Building images..."
    cd "$PROJECT_ROOT"

    # Binder + Dispatcher
    log "  binder..."
    docker build --target binder -t para-scheduler/binder:latest -f para-scheduler/Dockerfile .
    log "  dispatcher..."
    docker build --target dispatcher -t para-scheduler/dispatcher:latest -f para-scheduler/Dockerfile .

    # kube-scheduler
    log "  kube-scheduler..."
    cd k8s-scheduler
    make -f build/root/Makefile WHAT=cmd/kube-scheduler
    docker build -t para-scheduler/kube-scheduler:latest \
        -f - _output/bin <<'DOCKERFILE'
FROM gcr.io/distroless/static:nonroot
COPY kube-scheduler /usr/local/bin/kube-scheduler
ENTRYPOINT ["/usr/local/bin/kube-scheduler"]
DOCKERFILE
    cd "$PROJECT_ROOT"
    log "Images built."
}

# ---------- cluster ----------

do_cluster() {
    log "Creating Kind cluster (v1.33.4)..."
    kind create cluster --name "$CLUSTER_NAME" --config "$SCRIPT_DIR/kind-cluster.yaml"

    log "Loading images into Kind..."
    kind load docker-image para-scheduler/binder:latest       --name "$CLUSTER_NAME"
    kind load docker-image para-scheduler/dispatcher:latest    --name "$CLUSTER_NAME"
    kind load docker-image para-scheduler/kube-scheduler:latest --name "$CLUSTER_NAME"
}

# ---------- deploy ----------

do_deploy() {
    local num_schedulers="${1:-3}"

    log "Applying CRDs..."
    kubectl --context "$CTX" apply -f "$PROJECT_ROOT/para-sched-api/config/crd/bases/"

    log "Applying namespace + RBAC..."
    kubectl --context "$CTX" apply -f "$SCRIPT_DIR/namespace.yaml"
    kubectl --context "$CTX" apply -f "$SCRIPT_DIR/rbac.yaml"
    kubectl --context "$CTX" apply -f "$SCRIPT_DIR/rbac-system.yaml"

    log "Creating initial CRs..."
    kubectl --context "$CTX" apply -f "$SCRIPT_DIR/adoption-stats-init.yaml"

    log "Creating scheduler config..."
    kubectl --context "$CTX" apply -f "$SCRIPT_DIR/scheduler-config.yaml"

    log "Deploying Binder..."
    kubectl --context "$CTX" apply -f "$SCRIPT_DIR/binder.yaml"

    log "Deploying Dispatcher..."
    local sched_names=""
    for i in $(seq 0 $((num_schedulers - 1))); do
        [ -n "$sched_names" ] && sched_names="$sched_names,"
        sched_names="${sched_names}sched-${i}"
    done
    sed "s/--scheduler-names=sched-0,sched-1,sched-2/--scheduler-names=$sched_names/" \
        "$SCRIPT_DIR/dispatcher.yaml" | kubectl --context "$CTX" apply -f -

    log "Deploying $num_schedulers Scheduler instances..."
    for i in $(seq 0 $((num_schedulers - 1))); do
        SCHED_INDEX="$i" envsubst '$SCHED_INDEX' \
            < "$SCRIPT_DIR/scheduler-template.yaml" \
            | kubectl --context "$CTX" apply -f -
    done

    log "Waiting for rollout..."
    kubectl --context "$CTX" -n para-system rollout status deploy/para-binder --timeout=120s
    kubectl --context "$CTX" -n para-system rollout status deploy/para-dispatcher --timeout=120s
    for i in $(seq 0 $((num_schedulers - 1))); do
        kubectl --context "$CTX" -n para-system rollout status "deploy/para-scheduler-${i}" --timeout=120s
    done

    echo ""
    echo "============================================"
    echo "  Para-Sched deployed on $CTX"
    echo "  Binder: 1   Dispatcher: 1   Schedulers: $num_schedulers"
    echo "  Verify: kubectl --context $CTX -n para-system get pods"
    echo "============================================"
}

# ---------- main ----------

case "${1:-}" in
    --teardown)
        log "Deleting Kind cluster '$CLUSTER_NAME'..."
        kind delete cluster --name "$CLUSTER_NAME"
        ;;
    --build-only)
        do_build
        ;;
    --deploy-only)
        do_deploy "${2:-3}"
        ;;
    *)
        NUM="${1:-3}"
        do_build
        do_cluster
        do_deploy "$NUM"
        ;;
esac
