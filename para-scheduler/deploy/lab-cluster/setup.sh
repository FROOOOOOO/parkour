#!/bin/bash
# Setup script for lab cluster (1master+3worker, K8s v1.33.5).
#
# Prerequisites:
#   - kubectl configured to access the lab cluster
#   - Images imported to containerd on master node
#   - KWOK installed on the cluster (for virtual nodes)
#
# Usage:
#   ./setup.sh [NUM_SCHEDULERS]            # deploy (default: 10 schedulers)
#   ./setup.sh --teardown                  # full teardown: namespace + RBAC + CRD instances
#   ./setup.sh --teardown --keep-crd       # teardown but keep CRDs
#   ./setup.sh --clean                     # experiment-level cleanup: delete pods/CRD instances, restart components
#   ./setup.sh --scale NUM_SCHEDULERS      # scale to a different scheduler count (teardown + redeploy)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
NAMESPACE="para-system"
MAX_SCHEDULERS=16  # NodePort 30090-30105

# ============================================================
#  Helper functions
# ============================================================

wait_for_delete() {
    local resource="$1" timeout="${2:-120}"
    echo "  Waiting for $resource to be fully deleted (timeout ${timeout}s)..."
    kubectl wait --for=delete "$resource" -n "$NAMESPACE" --timeout="${timeout}s" 2>/dev/null || true
}

delete_crd_instances() {
    echo "Cleaning up CRD instances (cluster-scoped)..."
    kubectl delete schedulerassignments.scheduling.parscheduler.io --all --ignore-not-found 2>/dev/null || true
    kubectl delete parsyncconfigs.scheduling.parscheduler.io --all --ignore-not-found 2>/dev/null || true
    kubectl delete adoptionstats.scheduling.parscheduler.io --all --ignore-not-found 2>/dev/null || true
}

delete_snapshot_configmaps() {
    echo "Cleaning up ParSync snapshot ConfigMaps..."
    kubectl -n "$NAMESPACE" delete configmap -l app=parasched-snapshot --ignore-not-found 2>/dev/null || true
    for i in $(seq 0 20); do
        kubectl -n "$NAMESPACE" delete configmap "parasched-snapshot-${i}" --ignore-not-found 2>/dev/null || true
    done
}

delete_kwok_pods() {
    echo "Cleaning up kwok-pods (if any)..."
    # Delete pods on kwok-nodes (annotation-based); skip if none exist.
    local count
    count=$(kubectl get pods --all-namespaces -l 'type=kwok-pod' --no-headers 2>/dev/null | wc -l)
    if [ "$count" -gt 0 ]; then
        echo "  Deleting $count kwok-pods..."
        kubectl delete pods -l 'type=kwok-pod' --all-namespaces --ignore-not-found --wait=false
    else
        echo "  No kwok-pods found."
    fi
}

delete_schedulers() {
    # Delete all scheduler deployments and their metrics services (0..MAX_SCHEDULERS-1).
    echo "Deleting scheduler deployments and metrics services..."
    for i in $(seq 0 $((MAX_SCHEDULERS - 1))); do
        kubectl delete deployment "para-scheduler-${i}" -n "$NAMESPACE" --ignore-not-found 2>/dev/null &
        kubectl delete service "scheduler-metrics-${i}" -n "$NAMESPACE" --ignore-not-found 2>/dev/null &
    done
    wait  # wait for all background deletes
}

restart_components() {
    echo "Restarting all para-sched components..."
    # Restart schedulers — critical for clearing stale cache/snapshot state.
    # When consecutive experiments share identical scheduler args (e.g. P2 same→P3 diff
    # only changes the Dispatcher's --sync-pattern), kubectl patch does NOT trigger a
    # rolling restart, so the scheduler carries over stale internal state.
    local sched_count
    sched_count=$(kubectl -n "$NAMESPACE" get deployments -l app=para-scheduler --no-headers 2>/dev/null | wc -l)
    for i in $(seq 0 $((sched_count - 1))); do
        kubectl rollout restart deployment/para-scheduler-${i} -n "$NAMESPACE" 2>/dev/null || true
    done
    kubectl rollout restart deployment/para-binder -n "$NAMESPACE" 2>/dev/null || true
    kubectl rollout restart deployment/para-dispatcher -n "$NAMESPACE" 2>/dev/null || true
    for i in $(seq 0 $((sched_count - 1))); do
        kubectl rollout status deployment/para-scheduler-${i} -n "$NAMESPACE" --timeout=60s 2>/dev/null || true
    done
    kubectl rollout status deployment/para-binder -n "$NAMESPACE" --timeout=60s 2>/dev/null || true
    kubectl rollout status deployment/para-dispatcher -n "$NAMESPACE" --timeout=60s 2>/dev/null || true
}

# ============================================================
#  --teardown: Full environment teardown
# ============================================================

if [ "$1" = "--teardown" ]; then
    KEEP_CRD=false
    [ "$2" = "--keep-crd" ] && KEEP_CRD=true

    echo "============================================"
    echo "Para-Sched Full Teardown"
    echo "============================================"

    # 1. Delete CRD instances (cluster-scoped, not removed by namespace deletion)
    delete_crd_instances

    # 2. Delete snapshot ConfigMaps BEFORE namespace deletion.
    #    Namespace deletion will also remove them, but if it gets stuck (e.g.
    #    finalizers), having already deleted the ConfigMaps prevents a stale
    #    Binder from recreating them during the grace period.
    delete_snapshot_configmaps

    # 3. Delete namespace (takes all Deployments, Services, ConfigMaps, Pods with it)
    echo "Deleting namespace $NAMESPACE..."
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=false
    wait_for_delete "namespace/$NAMESPACE" 180

    # 4. Delete cluster-level RBAC
    echo "Deleting ClusterRole and ClusterRoleBinding..."
    kubectl delete clusterrole para-scheduler --ignore-not-found
    kubectl delete clusterrolebinding para-scheduler --ignore-not-found

    # 5. Optionally delete CRDs themselves
    if [ "$KEEP_CRD" = false ]; then
        echo "Deleting CRDs..."
        kubectl delete crd schedulerassignments.scheduling.parscheduler.io --ignore-not-found
        kubectl delete crd parsyncconfigs.scheduling.parscheduler.io --ignore-not-found
        kubectl delete crd adoptionstats.scheduling.parscheduler.io --ignore-not-found
        # Legacy CRD name from the transitional period when +resourceName=adoptionstats
        # was not yet set; harmless no-op if absent. Kept for a few releases so older
        # clusters that still have the orphan NotAccepted CRD get cleaned up too.
        kubectl delete crd adoptionstatses.scheduling.parscheduler.io --ignore-not-found
    else
        echo "CRDs preserved (--keep-crd)."
    fi

    echo ""
    echo "Teardown complete."
    exit 0
fi

# ============================================================
#  --clean: Experiment-level cleanup (between experiment runs)
# ============================================================

if [ "$1" = "--clean" ]; then
    echo "============================================"
    echo "Para-Sched Experiment Cleanup"
    echo "============================================"

    # 1. Delete all kwok-pods scheduled by previous experiment
    delete_kwok_pods

    # 2. Clean up CRD instances (ParSyncConfig, SchedulerAssignment, AdoptionStats)
    #    These will be re-created by Dispatcher on restart.
    delete_crd_instances

    # 3. Stop Binder BEFORE deleting ConfigMaps to prevent stale snapshot recreation.
    #    The old Binder's heartbeat (1s interval) recreates ConfigMaps after deletion
    #    if it's still running — carrying stale resource data into the next experiment.
    echo "Stopping Binder before cleaning ConfigMaps..."
    kubectl -n "$NAMESPACE" scale deployment/para-binder --replicas=0 --timeout=60s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=para-binder --timeout=60s 2>/dev/null || true

    # 4. Delete stale ParSync snapshot ConfigMaps (safe — Binder is stopped).
    delete_snapshot_configmaps

    # 5. Restart all components (Binder scaled back to 1, schedulers + dispatcher restarted)
    #    to reset in-memory state (cache, snapshot, conflict stats, partition manager, queues)
    kubectl -n "$NAMESPACE" scale deployment/para-binder --replicas=1 2>/dev/null || true
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
        echo "ERROR: max $MAX_SCHEDULERS schedulers (NodePort limit 30090-30105)."
        exit 1
    fi

    echo "============================================"
    echo "Para-Sched Scale: $NUM_SCHEDULERS schedulers"
    echo "============================================"

    # 1. Delete all existing scheduler deployments + services
    delete_schedulers

    # 2. Clean up CRD instances (will be re-created with new scheduler count)
    delete_crd_instances

    # 3. Stop Binder and clean snapshot ConfigMaps (same rationale as --clean)
    echo "Stopping Binder before cleaning ConfigMaps..."
    kubectl -n "$NAMESPACE" scale deployment/para-binder --replicas=0 --timeout=60s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=para-binder --timeout=60s 2>/dev/null || true
    delete_snapshot_configmaps

    # 4. Update Dispatcher with new scheduler names
    echo "Updating Dispatcher..."
    SCHED_NAMES=""
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        [ -n "$SCHED_NAMES" ] && SCHED_NAMES="$SCHED_NAMES,"
        SCHED_NAMES="${SCHED_NAMES}sched-${i}"
    done
    SCHED_NAMES="$SCHED_NAMES" envsubst '$SCHED_NAMES' \
        < "$SCRIPT_DIR/dispatcher.yaml" | kubectl apply -f -
    kubectl rollout restart deployment/para-dispatcher -n "$NAMESPACE"

    # 5. Deploy new scheduler instances + metrics services
    echo "Deploying $NUM_SCHEDULERS scheduler instances..."
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        SCHED_INDEX="$i" envsubst '$SCHED_INDEX' \
            < "$SCRIPT_DIR/scheduler-template.yaml" | \
            kubectl apply -f -
    done
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        SCHED_INDEX="$i" SCHED_NODE_PORT=$((30090 + i)) \
            envsubst '$SCHED_INDEX $SCHED_NODE_PORT' \
            < "$SCRIPT_DIR/scheduler-metrics-svc-template.yaml" | \
            kubectl apply -f -
    done

    # 6. Re-create AdoptionStats CR (deleted in step 2, Binder needs it)
    kubectl apply -f "$SCRIPT_DIR/adoption-stats.yaml" 2>/dev/null || true

    # 7. Restart Binder (scale back to 1) and all other components
    kubectl -n "$NAMESPACE" scale deployment/para-binder --replicas=1 2>/dev/null || true
    restart_components

    # 8. Wait for readiness
    echo "Waiting for scheduler deployments..."
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        kubectl -n "$NAMESPACE" rollout status deployment/para-scheduler-${i} --timeout=180s
    done

    echo ""
    echo "Scaled to $NUM_SCHEDULERS schedulers."
    exit 0
fi

# ============================================================
#  Default: Deploy
# ============================================================

NUM_SCHEDULERS=${1:-10}

if [ "$NUM_SCHEDULERS" -gt "$MAX_SCHEDULERS" ]; then
    echo "ERROR: max $MAX_SCHEDULERS schedulers (NodePort limit 30090-30105)."
    exit 1
fi

echo "============================================"
echo "Para-Sched Lab Cluster Setup"
echo "============================================"
echo "Schedulers:  $NUM_SCHEDULERS"
echo ""

# Step 1: Verify cluster access
echo "Step 1: Verifying cluster access..."
kubectl cluster-info
KUBE_VERSION=$(kubectl version -o json 2>/dev/null | python3 -c "import sys,json; v=json.load(sys.stdin)['serverVersion']; print(f'{v[\"major\"]}.{v[\"minor\"]}')" 2>/dev/null || echo "unknown")
echo "Kubernetes version: $KUBE_VERSION"
echo ""

# Step 2: Apply CRDs
echo "Step 2: Applying CRDs..."
if [ -d "$PROJECT_ROOT/para-sched-api/config/crd/bases" ]; then
    kubectl apply -f "$PROJECT_ROOT/para-sched-api/config/crd/bases/"
else
    echo "WARNING: CRD manifests not found at $PROJECT_ROOT/para-sched-api/config/crd/bases/. Apply manually."
fi
echo ""

# Step 3: Apply namespace and RBAC
echo "Step 3: Applying namespace and RBAC..."
kubectl apply -f "$SCRIPT_DIR/namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/rbac.yaml"
echo ""

# Step 3.5: Apply scheduler ConfigMap + AdoptionStats CR
echo "Step 3.5: Applying scheduler ConfigMap and AdoptionStats..."
kubectl apply -f "$SCRIPT_DIR/scheduler-config.yaml"
kubectl apply -f "$SCRIPT_DIR/adoption-stats.yaml"
echo ""

# Step 4: Deploy Binder
echo "Step 4: Deploying Binder..."
kubectl apply -f "$SCRIPT_DIR/binder.yaml"
echo ""

# Step 5: Deploy Dispatcher
echo "Step 5: Deploying Dispatcher..."
SCHED_NAMES=""
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    [ -n "$SCHED_NAMES" ] && SCHED_NAMES="$SCHED_NAMES,"
    SCHED_NAMES="${SCHED_NAMES}sched-${i}"
done

SCHED_NAMES="$SCHED_NAMES" envsubst '$SCHED_NAMES' \
    < "$SCRIPT_DIR/dispatcher.yaml" | kubectl apply -f -
echo ""

# Step 6: Deploy N Scheduler instances (from template)
echo "Step 6: Deploying $NUM_SCHEDULERS Scheduler instances..."
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    SCHED_INDEX="$i" envsubst '$SCHED_INDEX' \
        < "$SCRIPT_DIR/scheduler-template.yaml" | \
        kubectl apply -f -
done
echo ""

# Step 6.5: Deploy metrics NodePort Services
echo "Step 6.5: Deploying metrics NodePort Services..."
kubectl apply -f "$SCRIPT_DIR/metrics-services.yaml"
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    SCHED_INDEX="$i" SCHED_NODE_PORT=$((30090 + i)) \
        envsubst '$SCHED_INDEX $SCHED_NODE_PORT' \
        < "$SCRIPT_DIR/scheduler-metrics-svc-template.yaml" | \
        kubectl apply -f -
done
echo ""

# Step 7: Wait for readiness
echo "Step 7: Waiting for all deployments..."
kubectl -n "$NAMESPACE" rollout status deployment/para-binder --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/para-dispatcher --timeout=180s
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    kubectl -n "$NAMESPACE" rollout status deployment/para-scheduler-${i} --timeout=180s
done

echo ""
echo "============================================"
echo "Lab cluster setup complete!"
echo ""
echo "Components deployed in namespace: $NAMESPACE"
echo "  Binder:      1 replica (on control-plane)"
echo "  Dispatcher:  1 replica (on control-plane)"
echo "  Schedulers:  ${NUM_SCHEDULERS} instances (on control-plane)"
echo ""
echo "Metrics NodePort mapping (replace <master-node-ip> with your master node IP):"
echo "  Binder:      <master-node-ip>:30080/metrics"
echo "  Dispatcher:  <master-node-ip>:30081/metrics"
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    echo "  sched-${i}:    <master-node-ip>:$((30090 + i))/metrics"
done
echo ""
echo "Management commands:"
echo "  ./setup.sh --clean                 # reset between experiments"
echo "  ./setup.sh --scale 4              # change scheduler count"
echo "  ./setup.sh --teardown             # full teardown (incl. CRDs)"
echo "  ./setup.sh --teardown --keep-crd  # teardown, preserve CRDs"
echo "============================================"
