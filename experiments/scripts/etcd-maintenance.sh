#!/bin/bash
#
# etcd compact + defrag: prevents the etcd DB from growing unboundedly during experiments.
#
# Background: kubeadm K8s runs auto-compact every 5 min (logical versions only);
# defrag is never triggered automatically, so the on-disk DB file grows continuously.
# In practice, after long batch runs the DB grows from ~100 MB to 10 GB+,
# slowing apiserver list/watch responses and dropping ReplicaSet controller
# pod-creation QPS — CL2 step 04 (waiting for pods running) stretches from 30s to 70s.
#
# Usage:
#   ./etcd-maintenance.sh                 # compact + defrag
#   ./etcd-maintenance.sh --defrag-only   # defrag only (faster)
#   ./etcd-maintenance.sh --status        # show DB size only
#
# Environment variable overrides:
#   ETCD_POD=etcd-<HOSTNAME>   # etcd pod name (default: etcd-<HOSTNAME>)
#   ETCD_NS=kube-system         # namespace containing the etcd pod
#
# Requires: kubectl exec permission and jq on the host

set -e

ETCD_POD="${ETCD_POD:-etcd-<HOSTNAME>}"
ETCD_NS="${ETCD_NS:-kube-system}"

MODE="full"
case "${1:-}" in
    --defrag-only) MODE="defrag" ;;
    --status)      MODE="status" ;;
    ""|--full)     MODE="full" ;;
    -h|--help)
        sed -n '1,30p' "$0"; exit 0 ;;
    *)
        echo "Unknown option: $1"; exit 1 ;;
esac

# Shared cert flags for all etcdctl invocations
ETCDCTL="ETCDCTL_API=3 etcdctl \
    --cacert=/etc/kubernetes/pki/etcd/ca.crt \
    --cert=/etc/kubernetes/pki/etcd/server.crt \
    --key=/etc/kubernetes/pki/etcd/server.key"

echo "============================================"
echo "etcd maintenance (pod=$ETCD_POD, mode=$MODE)"
echo "============================================"

show_status() {
    echo ""
    kubectl -n "$ETCD_NS" exec "$ETCD_POD" -- sh -c "$ETCDCTL endpoint status --write-out=table"
}

echo "[before]"
show_status

if [ "$MODE" = "status" ]; then
    exit 0
fi

# compact all historical revisions up to the current one
if [ "$MODE" = "full" ]; then
    echo ""
    echo "[compact] Fetching current revision..."
    REV=$(kubectl -n "$ETCD_NS" exec "$ETCD_POD" -- sh -c "$ETCDCTL endpoint status --write-out=json" \
        | jq -r '.[0].Status.header.revision')
    if [ -z "$REV" ] || ! [[ "$REV" =~ ^[0-9]+$ ]]; then
        echo "ERROR: failed to parse revision (got '$REV')"
        exit 1
    fi
    echo "[compact] Compacting up to revision $REV..."
    kubectl -n "$ETCD_NS" exec "$ETCD_POD" -- sh -c "$ETCDCTL compact $REV" || {
        echo "WARNING: compact returned non-zero (may already be at this rev)"
    }
fi

# defrag: reclaim physical disk space from fragmented DB file
echo ""
echo "[defrag] Defragmenting etcd DB (may take 10-60s)..."
kubectl -n "$ETCD_NS" exec "$ETCD_POD" -- sh -c "$ETCDCTL defrag"

echo ""
echo "[after]"
show_status
