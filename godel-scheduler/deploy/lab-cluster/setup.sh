#!/bin/bash
# Setup script for Godel baseline on the lab cluster (1master+3worker, K8s v1.33.5).
#
# Maps to experiments/experiment-design.md baselines:
#   E1 (single Godel scheduler)  ─ ./setup.sh 1   or  ./setup.sh --scale 1
#   E2 (Godel-vanilla N parallel) ─ ./setup.sh 10  or  ./setup.sh --scale 10
#
# Prerequisites:
#   - kubectl configured to access the lab cluster (B node as master)
#   - godel-local:latest image already loaded on the control-plane node's
#     containerd (built via `cd godel-scheduler && make docker-images`)
#   - KWOK installed on Node A for virtual nodes (only needed at experiment time)
#
# Usage:
#   ./setup.sh [N]                  # deploy (default N=10 schedulers)
#   ./setup.sh --teardown           # full teardown: namespace + RBAC + CRDs
#   ./setup.sh --teardown --keep-crd
#   ./setup.sh --clean              # experiment-level cleanup, restart components
#   ./setup.sh --scale N            # change scheduler count without full teardown

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GODEL_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NAMESPACE="godel-system"
MAX_SCHEDULERS=10  # NodePort 30210-30219; expand here if N>10 ever needed
SCHED_NODE_PORT_BASE=30210

# ============================================================
#  Helper functions
# ============================================================

wait_for_delete() {
    local resource="$1" timeout="${2:-120}"
    echo "  Waiting for $resource to be fully deleted (timeout ${timeout}s)..."
    kubectl wait --for=delete "$resource" -n "$NAMESPACE" --timeout="${timeout}s" 2>/dev/null || true
}

apply_crds() {
    echo "Applying Godel CRDs..."
    # Idempotent — CRDs are cluster-scoped, safe to re-apply.
    if command -v kustomize >/dev/null 2>&1; then
        kustomize build "$GODEL_ROOT/manifests/base/crds" | kubectl apply -f -
    else
        kubectl apply -f "$GODEL_ROOT/manifests/base/crds/" 2>/dev/null \
            || { echo "ERROR: kustomize not found and direct apply failed"; exit 1; }
    fi
}

delete_godel_smoke_pods() {
    # Delete any pods scheduled by godel-scheduler that might linger between trials.
    # KWOK pods are labelled type=kwok-pod by the CL2 testoverride.
    echo "Cleaning up kwok-pods (if any)..."
    local count
    count=$(kubectl get pods --all-namespaces -l 'type=kwok-pod' --no-headers 2>/dev/null | wc -l)
    if [ "$count" -gt 0 ]; then
        echo "  Deleting $count kwok-pods..."
        kubectl delete pods -l 'type=kwok-pod' --all-namespaces --ignore-not-found --wait=false
    else
        echo "  No kwok-pods found."
    fi
}

delete_scheduler_crd_entries() {
    # Godel schedulers self-register into the schedulers CRD. Stale entries
    # from a previous N=10 run will linger if we scale down to N=1, making
    # the dispatcher think there are still 10 schedulers. Clean them out.
    echo "Cleaning up stale schedulers.scheduling.godel.kubewharf.io entries..."
    kubectl delete schedulers.scheduling.godel.kubewharf.io --all --ignore-not-found 2>/dev/null || true
}

delete_schedulers() {
    echo "Deleting scheduler deployments and metrics services..."
    for i in $(seq 0 $((MAX_SCHEDULERS - 1))); do
        kubectl delete deployment "godel-scheduler-${i}" -n "$NAMESPACE" --ignore-not-found 2>/dev/null &
        kubectl delete service "godel-scheduler-metrics-${i}" -n "$NAMESPACE" --ignore-not-found 2>/dev/null &
    done
    wait
    # Also remove the original `scheduler` deployment that the base manifest
    # `kustomize build manifests/base | kubectl apply -f -` would have created,
    # so this script can be run on top of a pristine base deployment.
    kubectl delete deployment scheduler -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
}

restart_components() {
    # Para-sched-aligned clean restart: scale-to-0 → wait-for-delete → scale-to-1.
    # This hard boundary prevents Prometheus from seeing overlapping time series
    # (old pod + new pod sharing the same instance label) which causes snapshot
    # diff undercounts. Compare para-scheduler's reset_scheduler_state() in
    # run-experiment.sh that uses the same pattern.
    echo "Restarting all godel components (scale-to-0 → scale-to-1)..."
    local sched_count
    sched_count=$(kubectl -n "$NAMESPACE" get deployments -l app=godel-scheduler --no-headers 2>/dev/null | wc -l)

    # Phase 1: Scale all components to 0
    echo "  Scaling all components to 0..."
    for i in $(seq 0 $((sched_count - 1))); do
        kubectl scale "deployment/godel-scheduler-${i}" -n "$NAMESPACE" --replicas=0 --timeout=60s 2>/dev/null || true
    done
    kubectl scale deployment/binder -n "$NAMESPACE" --replicas=0 --timeout=60s 2>/dev/null || true
    kubectl scale deployment/dispatcher -n "$NAMESPACE" --replicas=0 --timeout=60s 2>/dev/null || true
    kubectl scale deployment/controller-manager -n "$NAMESPACE" --replicas=0 --timeout=60s 2>/dev/null || true

    # Phase 2: Wait for all pods to be fully terminated before touching anything
    echo "  Waiting for pods to terminate..."
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=godel-scheduler --timeout=90s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=binder --timeout=90s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=dispatcher --timeout=90s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=controller-manager --timeout=90s 2>/dev/null || true

    # Phase 3: Scale back to 1 (fresh pods, fresh Prometheus counters)
    echo "  Scaling components back up..."
    for i in $(seq 0 $((sched_count - 1))); do
        kubectl scale "deployment/godel-scheduler-${i}" -n "$NAMESPACE" --replicas=1 2>/dev/null || true
    done
    kubectl scale deployment/binder -n "$NAMESPACE" --replicas=1 2>/dev/null || true
    kubectl scale deployment/dispatcher -n "$NAMESPACE" --replicas=1 2>/dev/null || true
    kubectl scale deployment/controller-manager -n "$NAMESPACE" --replicas=1 2>/dev/null || true

    # Phase 4: Wait for rollouts to complete
    echo "  Waiting for rollouts..."
    for i in $(seq 0 $((sched_count - 1))); do
        kubectl rollout status "deployment/godel-scheduler-${i}" -n "$NAMESPACE" --timeout=120s 2>/dev/null || true
    done
    kubectl rollout status deployment/binder -n "$NAMESPACE" --timeout=120s 2>/dev/null || true
    kubectl rollout status deployment/dispatcher -n "$NAMESPACE" --timeout=120s 2>/dev/null || true
    kubectl rollout status deployment/controller-manager -n "$NAMESPACE" --timeout=120s 2>/dev/null || true

    # Phase 5: Extra wait for Prometheus to scrape new pods at least twice.
    # Prometheus scrape interval is 15s; 35s provides 2+ scrapes + jitter margin.
    # This ensures snapshot-diff START queries see the new (near-zero) counters.
    echo "  Waiting 35s for Prometheus to scrape new components..."
    sleep 35
}

deploy_schedulers() {
    local n="$1"
    if ! command -v envsubst >/dev/null 2>&1; then
        echo "ERROR: envsubst not found (install gettext-base)"; exit 1
    fi
    echo "Deploying $n scheduler instances..."
    for i in $(seq 0 $((n - 1))); do
        SCHED_INDEX="$i" envsubst '$SCHED_INDEX' \
            < "$SCRIPT_DIR/scheduler-template.yaml" | kubectl apply -f -
    done
    echo "Deploying $n scheduler metrics NodePort services..."
    for i in $(seq 0 $((n - 1))); do
        SCHED_INDEX="$i" SCHED_NODE_PORT=$((SCHED_NODE_PORT_BASE + i)) \
            envsubst '$SCHED_INDEX $SCHED_NODE_PORT' \
            < "$SCRIPT_DIR/scheduler-metrics-svc-template.yaml" | kubectl apply -f -
    done
}

# ============================================================
#  --teardown: Full environment teardown
# ============================================================

if [ "$1" = "--teardown" ]; then
    KEEP_CRD=false
    [ "$2" = "--keep-crd" ] && KEEP_CRD=true

    echo "============================================"
    echo "Godel Baseline Full Teardown"
    echo "============================================"

    delete_scheduler_crd_entries

    echo "Deleting namespace $NAMESPACE..."
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=false
    wait_for_delete "namespace/$NAMESPACE" 180

    echo "Deleting ClusterRole and ClusterRoleBinding..."
    kubectl delete clusterrole godel --ignore-not-found
    kubectl delete clusterrolebinding godel --ignore-not-found

    if [ "$KEEP_CRD" = false ]; then
        echo "Deleting Godel CRDs..."
        kubectl delete crd schedulers.scheduling.godel.kubewharf.io --ignore-not-found
        kubectl delete crd podgroups.scheduling.godel.kubewharf.io --ignore-not-found
        kubectl delete crd movements.scheduling.godel.kubewharf.io --ignore-not-found
        kubectl delete crd reservations.scheduling.godel.kubewharf.io --ignore-not-found
        kubectl delete crd nmnodes.node.godel.kubewharf.io --ignore-not-found
        kubectl delete crd customnoderesources.node.katalyst.kubewharf.io --ignore-not-found
    else
        echo "CRDs preserved (--keep-crd)."
    fi

    echo ""
    echo "Teardown complete."
    exit 0
fi

# ============================================================
#  --clean: Experiment-level cleanup
# ============================================================

if [ "$1" = "--clean" ]; then
    echo "============================================"
    echo "Godel Baseline Experiment Cleanup"
    echo "============================================"

    delete_godel_smoke_pods
    delete_scheduler_crd_entries
    restart_components

    echo ""
    echo "Cleanup complete. Ready for next experiment run."
    exit 0
fi

# ============================================================
#  --scale N: Teardown schedulers and redeploy with new count
# ============================================================

if [ "$1" = "--scale" ]; then
    NUM_SCHEDULERS="${2:?Usage: ./setup.sh --scale NUM_SCHEDULERS}"
    if [ "$NUM_SCHEDULERS" -gt "$MAX_SCHEDULERS" ]; then
        echo "ERROR: max $MAX_SCHEDULERS schedulers (NodePort limit ${SCHED_NODE_PORT_BASE}+${MAX_SCHEDULERS}). Bump MAX_SCHEDULERS in this script if needed."
        exit 1
    fi

    echo "============================================"
    echo "Godel Baseline Scale: $NUM_SCHEDULERS schedulers"
    echo "============================================"

    delete_schedulers
    delete_scheduler_crd_entries

    deploy_schedulers "$NUM_SCHEDULERS"

    # Restart dispatcher (scale-to-0 → scale-to-1, para-sched aligned) so its
    # scheduler-maintainer re-discovers the new set with clean Prometheus counters.
    echo "  Restarting dispatcher..."
    kubectl scale deployment/dispatcher -n "$NAMESPACE" --replicas=0 --timeout=60s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=dispatcher --timeout=60s 2>/dev/null || true
    kubectl scale deployment/dispatcher -n "$NAMESPACE" --replicas=1 2>/dev/null || true
    kubectl -n "$NAMESPACE" rollout status deployment/dispatcher --timeout=120s

    echo "Waiting for scheduler deployments..."
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        kubectl -n "$NAMESPACE" rollout status "deployment/godel-scheduler-${i}" --timeout=180s
    done
    kubectl -n "$NAMESPACE" rollout status deployment/dispatcher --timeout=120s

    echo ""
    echo "Scaled to $NUM_SCHEDULERS schedulers."
    exit 0
fi

# ============================================================
#  Default: Deploy
# ============================================================

NUM_SCHEDULERS=${1:-10}

if [ "$NUM_SCHEDULERS" -gt "$MAX_SCHEDULERS" ]; then
    echo "ERROR: max $MAX_SCHEDULERS schedulers. Bump MAX_SCHEDULERS in this script if needed."
    exit 1
fi

echo "============================================"
echo "Godel Baseline Lab Cluster Setup"
echo "============================================"
echo "Schedulers:  $NUM_SCHEDULERS"
echo ""

echo "Step 1: Verifying cluster access..."
kubectl cluster-info
echo ""

echo "Step 2: Applying Godel CRDs..."
apply_crds
echo ""

echo "Step 3: Applying namespace, RBAC, and ConfigMaps..."
kubectl apply -f "$SCRIPT_DIR/namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/rbac.yaml"
kubectl apply -f "$SCRIPT_DIR/scheduler-config.yaml"
kubectl apply -f "$SCRIPT_DIR/binder-config.yaml"
echo ""

# Remove the single-replica `scheduler` deployment that the base manifest
# may have left behind (`kustomize build manifests/base | kubectl apply`),
# so the N-instance template-rendered deployments are the only schedulers.
echo "Step 4: Removing legacy single-replica scheduler if present..."
kubectl delete deployment scheduler -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
delete_scheduler_crd_entries
echo ""

echo "Step 5: Deploying Binder, Dispatcher, Controller Manager..."
kubectl apply -f "$SCRIPT_DIR/binder.yaml"
kubectl apply -f "$SCRIPT_DIR/dispatcher.yaml"
kubectl apply -f "$SCRIPT_DIR/controller-manager.yaml"
kubectl apply -f "$SCRIPT_DIR/metrics-services.yaml"
echo ""

echo "Step 6: Deploying $NUM_SCHEDULERS Scheduler instances + metrics services..."
deploy_schedulers "$NUM_SCHEDULERS"
echo ""

echo "Step 7: Waiting for all deployments..."
kubectl -n "$NAMESPACE" rollout status deployment/binder --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/dispatcher --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/controller-manager --timeout=180s
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    kubectl -n "$NAMESPACE" rollout status "deployment/godel-scheduler-${i}" --timeout=180s
done

echo ""
echo "============================================"
echo "Godel baseline setup complete!"
echo ""
echo "Components deployed in namespace: $NAMESPACE"
echo "  Binder:             1 replica (on control-plane)"
echo "  Dispatcher:         1 replica (on control-plane)"
echo "  Controller Manager: 1 replica (on control-plane)"
echo "  Schedulers:         ${NUM_SCHEDULERS} instances (on control-plane)"
echo ""
echo "Metrics NodePort mapping:"
echo "  Binder:             <MASTER_IP>:30200/metrics"
echo "  Dispatcher:         <MASTER_IP>:30201/metrics"
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    echo "  godel-sched-${i}:    <MASTER_IP>:$((SCHED_NODE_PORT_BASE + i))/metrics"
done
echo ""
echo "Management commands:"
echo "  ./setup.sh --clean                 # reset between experiments"
echo "  ./setup.sh --scale 1               # E1 (single scheduler)"
echo "  ./setup.sh --scale 10              # E2 (N=10 parallel)"
echo "  ./setup.sh --teardown              # full teardown"
echo "  ./setup.sh --teardown --keep-crd   # teardown, preserve CRDs"
echo "============================================"
