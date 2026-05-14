#!/bin/bash

# Cleanup script for Para-Sched experiments.
#
# Use this after aborting a run-experiment.sh to return the cluster to a
# clean state before starting the next experiment.
#
# What this script does (in order):
#   1. Delete saturation/latency workload Deployments
#   2. Force-delete any pods stuck on KWOK nodes + wait for removal
#   3. Delete KWOK nodes (--wait=true)
#   4. Clean KWOK leases + restart shards
#   5. Delete & wait for CL2 test namespaces (force-finalize if stuck)
#   6. Reset para-sched component state (setup.sh --clean)
#   7. Wait for API server to settle (etcd backlog drained)
#
# What this script does NOT do:
#   - Tear down para-sched Deployments (use setup.sh --teardown for that)
#   - Delete real worker-node workloads (board D)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEPLOY_DIR="$PROJECT_ROOT/para-scheduler/deploy/lab-cluster"
NAMESPACE="para-system"

# Optional: skip the setup.sh --clean step (e.g. para-system not deployed)
SKIP_PARASCHED_RESET=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-parasched-reset)
            SKIP_PARASCHED_RESET=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--skip-parasched-reset]"
            echo ""
            echo "Options:"
            echo "  --skip-parasched-reset   Skip 'setup.sh --clean' (use if para-system is not deployed)"
            echo ""
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "============================================"
echo "Para-Sched Cluster Cleanup"
echo "============================================"

# ── Step 1: Delete workload Deployments ─────────────────────────────────────
echo ""
echo "[1/7] Deleting saturation/latency workload Deployments..."
kubectl delete deployments -l group=saturation --all-namespaces \
    --ignore-not-found=true --wait=false 2>/dev/null || true
kubectl delete deployments -l group=latency --all-namespaces \
    --ignore-not-found=true --wait=false 2>/dev/null || true

# ── Step 2: Force-delete stuck pods in CL2 namespaces ───────────────────────
# Must happen before deleting KWOK nodes — pods on removed nodes can never
# gracefully terminate (no kubelet), causing namespaces to stuck in Terminating.
echo ""
echo "[2/7] Force-deleting pods in CL2 test namespaces (test-*)..."
CL2_NAMESPACES=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
    | tr ' ' '\n' | grep '^test-' || true)
if [ -n "$CL2_NAMESPACES" ]; then
    NS_COUNT=$(echo "$CL2_NAMESPACES" | wc -w | xargs)
    echo "  Force-deleting pods across $NS_COUNT namespaces in parallel..."
    for ns in $CL2_NAMESPACES; do
        kubectl delete pods --all -n "$ns" \
            --force --grace-period=0 --ignore-not-found=true 2>/dev/null &
    done
    wait

    # Wait for pods to actually disappear from etcd (force-delete is async).
    echo "  Waiting for test pods to be fully removed..."
    POD_WAIT_TIMEOUT=180
    POD_WAIT_START=$(date +%s)
    while true; do
        REMAINING=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
        if [ "$REMAINING" -eq 0 ]; then
            echo "  All test pods removed."
            break
        fi
        POD_ELAPSED=$(( $(date +%s) - POD_WAIT_START ))
        if [ $POD_ELAPSED -ge $POD_WAIT_TIMEOUT ]; then
            echo "  Pod cleanup timeout (${POD_WAIT_TIMEOUT}s), $REMAINING pods remaining — proceeding."
            break
        fi
        echo "  Waiting for $REMAINING pods to be removed... (${POD_ELAPSED}s/${POD_WAIT_TIMEOUT}s)"
        sleep 10
    done
else
    echo "  No CL2 namespaces found."
fi

# ── Step 3: Delete KWOK nodes ────────────────────────────────────────────────
echo ""
echo "[3/7] Deleting KWOK nodes (label type=kwok)..."
KWOK_COUNT=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l | xargs)
if [ "$KWOK_COUNT" -gt 0 ]; then
    echo "  Deleting $KWOK_COUNT KWOK nodes (waiting for completion)..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
else
    echo "  No KWOK nodes found."
fi

# ── Step 4: Clean KWOK leases and restart shards ─────────────────────────────
# Delete stale lease objects.  When nodes are deleted and recreated, lease UIDs
# change while KWOK caches old UIDs → "Precondition failed" on renewal.
# Note: run-experiment.sh Step 1a also restarts KWOK shards immediately before
# node creation to ensure a fresh Watch connection.  The restart here cleans up
# after an aborted experiment; the restart in Step 1a ensures timing correctness.
echo ""
echo "[4/7] Cleaning KWOK node leases and restarting shards..."
KWOK_LEASES=$(kubectl -n kube-node-lease get leases -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
    | tr ' ' '\n' | grep '^kwok-' || true)
if [ -n "$KWOK_LEASES" ]; then
    echo "  Deleting $(echo "$KWOK_LEASES" | wc -w | xargs) stale KWOK leases..."
    echo "$KWOK_LEASES" | xargs kubectl -n kube-node-lease delete lease --ignore-not-found=true 2>/dev/null || true
else
    echo "  No KWOK leases found."
fi

echo "  Restarting KWOK shards..."
sudo systemctl restart kwok kwok{1..9} 2>/dev/null || true
sleep 5

# ── Step 5: Delete CL2 test namespaces and wait ──────────────────────────────
echo ""
echo "[5/7] Cleaning up CL2 test namespaces..."
CL2_NS_TO_DELETE=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^test-' || true)
for ns in $CL2_NS_TO_DELETE; do
    kubectl delete ns "$ns" --ignore-not-found=true --wait=false 2>/dev/null || true
done

CLEANUP_TIMEOUT=120
CLEANUP_START=$(date +%s)
while true; do
    TERMINATING_NS=$(kubectl get ns --field-selector=status.phase=Terminating \
        -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
    CL2_TERMINATING=""
    for ns in $TERMINATING_NS; do
        case "$ns" in test-*) CL2_TERMINATING="$CL2_TERMINATING $ns" ;; esac
    done
    CL2_TERMINATING=$(echo "$CL2_TERMINATING" | xargs)

    if [ -z "$CL2_TERMINATING" ]; then
        echo "  All CL2 test namespaces cleaned up."
        break
    fi

    ELAPSED=$(( $(date +%s) - CLEANUP_START ))
    if [ $ELAPSED -ge $CLEANUP_TIMEOUT ]; then
        echo "  Timeout — force-finalizing stuck namespaces..."
        for ns in $CL2_TERMINATING; do
            kubectl get ns "$ns" -o json 2>/dev/null \
                | jq '.spec.finalizers = []' \
                | kubectl replace --raw "/api/v1/namespaces/$ns/finalize" -f - 2>/dev/null || true
        done
        sleep 5
        break
    fi

    COUNT=$(echo "$CL2_TERMINATING" | wc -w | xargs)
    echo "  Waiting for $COUNT namespace(s) to terminate... (${ELAPSED}s/${CLEANUP_TIMEOUT}s)"
    sleep 10
done

# ── Step 6: Reset para-sched component state ─────────────────────────────────
echo ""
if [ "$SKIP_PARASCHED_RESET" = "true" ]; then
    echo "[6/7] Skipping para-sched reset (--skip-parasched-reset)."
else
    echo "[6/7] Resetting para-sched state (CRDs + snapshot ConfigMaps + all components restart)..."
    "$DEPLOY_DIR/setup.sh" --clean || echo "  WARNING: setup.sh --clean failed (non-fatal)"
fi

# ── Step 7: Wait for API server to settle ────────────────────────────────────
# After large-scale cleanup, etcd may still be processing async deletions
# (garbage collection, etc.). Poll until cluster state is fully clean.
echo ""
echo "[7/7] Waiting for API server to settle..."
SETTLE_TIMEOUT=300
SETTLE_START=$(date +%s)
while true; do
    ELAPSED=$(( $(date +%s) - SETTLE_START ))
    if [ $ELAPSED -ge $SETTLE_TIMEOUT ]; then
        echo "  Settle timeout (${SETTLE_TIMEOUT}s) — proceeding anyway."
        break
    fi

    KWOK_NODES=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l)
    REMAINING_PODS=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
    API_OK=false
    if timeout 5 kubectl get ns default >/dev/null 2>&1; then
        API_OK=true
    fi

    if [ "$KWOK_NODES" -eq 0 ] && [ "$REMAINING_PODS" -eq 0 ] && [ "$API_OK" = true ]; then
        echo "  Cluster settled (${ELAPSED}s): 0 KWOK nodes, 0 test pods, API responsive."
        break
    fi

    echo "  Settling... (${ELAPSED}s) kwok_nodes=$KWOK_NODES test_pods=$REMAINING_PODS api_ok=$API_OK"
    sleep 10
done

echo ""
echo "============================================"
echo "Cluster cleanup complete."
echo "============================================"
