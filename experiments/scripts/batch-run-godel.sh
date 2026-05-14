#!/bin/bash

# ============================================================
# Godel baseline batch experiment script (boards B1/B2/B3)
#
# Mirrors batch-run.sh but only runs the Godel E1 (N=1) / E2 (N=10/N-sweep)
# two baselines. Godel has no proposed-method parameters (no K/penalty/strategy/
# sync/partitions), so there are no board-A-optima dependencies and no C/D/E boards.
#
# Structure:
#   - B1   Low-contention scale sweep  (E1 + E2 × 3 scales)
#   - B2   High-contention scale sweep (E1 + E2 × 4 scales)
#   - B3   High-contention scheduler sweep (E2 × 5 N values @ 10k nodes)
#
# Prerequisites:
#   - godel-scheduler/deploy/lab-cluster/setup.sh has been run (CRD/RBAC installed)
#   - Prometheus has godel scrape jobs configured
#   - run-godel-baseline.sh / collect-metrics-godel.sh / kwok-deployment-godel.yaml are in place
# ============================================================

set -e

# Bypass any HTTP(S) proxy for the K8s cluster network + Prometheus on Node A.
# Without this, kubectl / curl honor shell-level HTTPS_PROXY (e.g. Clash on
# 127.0.0.1:7890) and fail with "proxyconnect tcp: connect: connection refused"
# when the proxy is offline. Child processes (run-godel-baseline.sh, godel
# setup.sh, kubectl, curl) all inherit these via exec.
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}<YOUR_CLUSTER_SUBNET>/24,127.0.0.1,localhost,kubernetes.default,kubernetes.default.svc,.svc,.svc.cluster.local"
export no_proxy="$NO_PROXY"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GODEL_DEPLOY_DIR="$PROJECT_ROOT/godel-scheduler/deploy/lab-cluster"

# ========== Default parameters ==========
DRY_RUN=false
EXPERIMENT_GROUP="all"
TRIALS=3
COLLECT_LOGS=false
DEFRAG_BETWEEN=true
FINAL_CLEANUP=true
SKIP_WARMUP=false
VARIANCE="0.6"            # HC-V default (aligned with batch-run.sh)
PROMETHEUS_URL="http://${MONITORING_IP:-<MONITORING_IP>}:9091"

while [[ $# -gt 0 ]]; do
    case $1 in
        --group)             EXPERIMENT_GROUP="$2"; shift 2 ;;
        --trials)            TRIALS="$2"; shift 2 ;;
        --dry-run)           DRY_RUN=true; shift ;;
        --collect-logs)      COLLECT_LOGS=true; shift ;;
        --no-defrag)         DEFRAG_BETWEEN=false; shift ;;
        --no-final-cleanup)  FINAL_CLEANUP=false; shift ;;
        --skip-warmup)       SKIP_WARMUP=true; shift ;;
        --variance)          VARIANCE="$2"; shift 2 ;;
        --prometheus-url)    PROMETHEUS_URL="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --group <name> [options]

Run Godel baseline boards B1/B2/B3.

Options:
  --group GROUP          Experiment group:
                           all          = B1 + B2 + B3
                           B1 / B2 / B3 individual board
                         (default: all)
  --trials N             Repetitions per group (default: 3)
  --variance V           Capacity-variance level: 0 | 0.3 | 0.6 | 1.0 (default: 0.6)
  --dry-run              Print experiment plan only
  --collect-logs         Collect scheduler/binder/dispatcher logs
  --no-defrag            Disable etcd defrag after each experiment (enabled by default)
  --no-final-cleanup     Disable node and environment cleanup at script end (for debugging)
  --skip-warmup          Skip scale-change warmup
  --prometheus-url URL   Prometheus URL (default: http://${MONITORING_IP:-<MONITORING_IP>}:9091)

Example:
  # Run all B1+B2+B3 (default 3 trials, V=0.6)
  $0 --group all --trials 3

  # Run B2 high-contention only
  $0 --group B2 --trials 3

  # Dry run to preview the plan
  $0 --group all --dry-run

EOF
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "============================================"
echo "Godel Baseline Batch Runner (B1/B2/B3)"
echo "============================================"
echo "Group:          $EXPERIMENT_GROUP"
echo "Trials:         $TRIALS"
echo "Variance (V):   $VARIANCE"
echo "Dry-run:        $DRY_RUN"
echo "Collect logs:   $COLLECT_LOGS"
echo "Prometheus:     $PROMETHEUS_URL"
echo ""

FAILED_EXPERIMENTS=()
TOTAL_RUN=0

# ========== Wait for cluster cleanup ==========
wait_for_clean_cluster() {
    local timeout=180 start=$(date +%s)
    while true; do
        local terminating
        terminating=$(kubectl get ns --field-selector=status.phase=Terminating -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
        local cl2_ns=""
        for ns in $terminating; do
            case "$ns" in test-*) cl2_ns="$cl2_ns $ns" ;; esac
        done
        cl2_ns=$(echo "$cl2_ns" | xargs)
        [ -z "$cl2_ns" ] && return
        local elapsed=$(( $(date +%s) - start ))
        if [ $elapsed -ge $timeout ]; then
            echo "  WARNING: ${timeout}s timeout — force-finalizing remaining CL2 namespaces..."
            for ns in $cl2_ns; do
                kubectl get ns "$ns" -o json 2>/dev/null \
                    | jq '.spec.finalizers = []' \
                    | kubectl replace --raw "/api/v1/namespaces/$ns/finalize" -f - 2>/dev/null || true
            done
            sleep 5; return
        fi
        echo "  Waiting for $(echo "$cl2_ns" | wc -w | xargs) CL2 namespaces to terminate... (${elapsed}s/${timeout}s)"
        sleep 10
    done
}

# ========== etcd maintenance ==========
defrag_etcd() {
    echo "----------------------------------------"
    echo "etcd compact + defrag"
    echo "----------------------------------------"
    if ! bash "$SCRIPT_DIR/etcd-maintenance.sh" --full 2>&1 | tail -15; then
        echo "  WARNING: etcd maintenance failed; continuing."
    fi
}

# ========== Workload profiles ==========
LOW_PPN=29;  LOW_CPU="1000m";  LOW_MEM="8Gi"
HIGH_PPN=1;  HIGH_CPU="24000m"; HIGH_MEM="192Gi"

# ========== Warmup state ==========
# Track last warmup (nodes, schedulers) tuple — re-warm on either change.
# Variance is included for parity with batch-run.sh but rarely changes in Godel batch.
LAST_WARMUP_NODES=""
LAST_WARMUP_SCHEDS=""
LAST_WARMUP_VARIANCE=""

# warmup_at_nodes NODES SCHEDS — 1-trial throwaway at given scale; results deleted.
warmup_at_nodes() {
    local warm_nodes=$1
    local warm_scheds=${2:-10}
    local warm_name="godel-warmup-${warm_nodes}n-N${warm_scheds}"
    echo "----------------------------------------"
    echo "Warmup: ${warm_nodes} nodes, N=${warm_scheds}, V=${VARIANCE} (results discarded)"
    echo "----------------------------------------"
    if "$SCRIPT_DIR/run-godel-baseline.sh" \
        --name "$warm_name" \
        --nodes "$warm_nodes" \
        --schedulers "$warm_scheds" \
        --trials 1 \
        --pods-per-node "$HIGH_PPN" \
        --cpu-request "$HIGH_CPU" \
        --memory-request "$HIGH_MEM" \
        --variance "$VARIANCE" \
        --prometheus-url "$PROMETHEUS_URL" \
        --preserve-nodes 2>&1 | tail -30; then
        echo "  Warmup OK (${warm_nodes}n N=${warm_scheds})"
    else
        echo "  Warmup FAILED (${warm_nodes}n N=${warm_scheds}) — real experiments will still run"
    fi
    # Discard the warmup results directory (matches *-godel-N{N}-{nodes}n suffix).
    rm -rf "$PROJECT_ROOT/experiments/results/"*"-${warm_name}-godel-"*"n"
    wait_for_clean_cluster
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
    LAST_WARMUP_NODES="$warm_nodes"
    LAST_WARMUP_SCHEDS="$warm_scheds"
    LAST_WARMUP_VARIANCE="$VARIANCE"
}

# maybe_warmup_before NODES SCHEDS — trigger warmup if (nodes, scheds, V) tuple
# differs from last warmup. Honors --skip-warmup.
maybe_warmup_before() {
    local target_nodes=$1
    local target_scheds=$2
    if [ "$SKIP_WARMUP" = true ]; then
        LAST_WARMUP_NODES="$target_nodes"
        LAST_WARMUP_SCHEDS="$target_scheds"
        LAST_WARMUP_VARIANCE="$VARIANCE"
        return
    fi
    if [ "$LAST_WARMUP_NODES" != "$target_nodes" ] \
       || [ "$LAST_WARMUP_SCHEDS" != "$target_scheds" ] \
       || [ "$LAST_WARMUP_VARIANCE" != "$VARIANCE" ]; then
        echo ">> Scale/N/V change: last=(${LAST_WARMUP_NODES:-<none>}n N=${LAST_WARMUP_SCHEDS:-<none>} V=${LAST_WARMUP_VARIANCE:-<none>}), next=(${target_nodes}n N=${target_scheds} V=${VARIANCE}) — inserting warmup."
        warmup_at_nodes "$target_nodes" "$target_scheds"
    fi
}

# ========== run helper ==========
# Args: name nodes scheds trials ppn cpu mem
run_godel_experiment() {
    local name=$1 nodes=$2 scheds=$3 trials=$4 ppn=$5 cpu=$6 mem=$7

    echo "----------------------------------------"
    echo "[$name] nodes=$nodes scheds=$scheds trials=$trials  workload: ppn=$ppn cpu=$cpu mem=$mem  V=$VARIANCE"
    echo "----------------------------------------"

    # Trigger warmup if (nodes, scheds, V) changed since last warmup.
    [ "$DRY_RUN" = false ] && maybe_warmup_before "$nodes" "$scheds"

    local extra=(
        --preserve-nodes
        --variance "$VARIANCE"
        --pods-per-node "$ppn"
        --cpu-request "$cpu"
        --memory-request "$mem"
        --prometheus-url "$PROMETHEUS_URL"
    )
    [ "$COLLECT_LOGS" = true ] && extra+=("--collect-logs")

    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY RUN] ./run-godel-baseline.sh --name $name --nodes $nodes --schedulers $scheds \\"
        echo "            --trials $trials ${extra[*]}"
        return
    fi

    TOTAL_RUN=$((TOTAL_RUN + 1))
    if "$SCRIPT_DIR/run-godel-baseline.sh" \
        --name "$name" \
        --nodes "$nodes" \
        --schedulers "$scheds" \
        --trials "$trials" \
        "${extra[@]}"; then
        echo "[$name] OK"
    else
        echo "[$name] FAILED (continuing)"
        FAILED_EXPERIMENTS+=("$name")
    fi
    wait_for_clean_cluster
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
}

# ========== final cleanup ==========
final_cleanup() {
    echo "========================================"
    echo "Final cleanup (script end)"
    echo "========================================"

    echo "Deleting all KWOK nodes..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
    local kwok_leases
    kwok_leases=$(kubectl -n kube-node-lease get leases -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^kwok-' || true)
    if [ -n "$kwok_leases" ]; then
        echo "$kwok_leases" | xargs kubectl -n kube-node-lease delete lease --ignore-not-found=true 2>/dev/null || true
    fi

    echo "Invoking godel setup.sh --clean to reset godel-system..."
    if [ -x "$GODEL_DEPLOY_DIR/setup.sh" ]; then
        bash "$GODEL_DEPLOY_DIR/setup.sh" --clean 2>&1 | tail -10 || true
    else
        echo "  (godel setup.sh not found at $GODEL_DEPLOY_DIR/setup.sh, skipping)"
    fi

    # Final defrag: deleting 10k+ KWOK nodes + scheduler CRD entries generates
    # etcd tombstones that the per-experiment defrag loop never sees.
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
    echo "Cluster is clean."
}

# ============================================================
# Board B1: Low-contention scale sweep
# Low workload (29 ppn, 1 CPU / 8Gi). Event-driven only.
# ============================================================
run_B1() {
    echo "=== Board B1: Low-contention scale sweep (Godel E1/E2) ==="
    for nodes in 1000 2000 5000; do
        run_godel_experiment "B1-${nodes}n-E1" $nodes 1  $TRIALS $LOW_PPN $LOW_CPU $LOW_MEM
        run_godel_experiment "B1-${nodes}n-E2" $nodes 10 $TRIALS $LOW_PPN $LOW_CPU $LOW_MEM
    done
}

# ============================================================
# Board B2: High-contention (HC-V V=0.6) scale sweep
# High workload (1 ppn, 24 CPU / 192Gi).
# ============================================================
run_B2() {
    echo "=== Board B2: High-contention scale sweep (Godel E1/E2) ==="
    for nodes in 2000 5000 10000 20000; do
        run_godel_experiment "B2-${nodes}n-E1" $nodes 1  $TRIALS $HIGH_PPN $HIGH_CPU $HIGH_MEM
        run_godel_experiment "B2-${nodes}n-E2" $nodes 10 $TRIALS $HIGH_PPN $HIGH_CPU $HIGH_MEM
    done
}

# ============================================================
# Board B3: High-contention scheduler sweep
# Fixed at 10000 nodes, N ∈ {2,4,6,8,10} (E2 vanilla at varying parallelism).
# Godel has no E3/P3/P4 proposed methods; only E2 is run. E1 (N=1) was already
# measured in B2-10000n-E1 and is not repeated here.
# ============================================================
run_B3() {
    echo "=== Board B3: High-contention scheduler sweep (Godel E2) ==="
    for n_sched in 2 4 6 8 10; do
        run_godel_experiment "B3-N${n_sched}-E2" 10000 $n_sched $TRIALS $HIGH_PPN $HIGH_CPU $HIGH_MEM
    done
}

# ============================================================
# Main dispatch
# ============================================================
case $EXPERIMENT_GROUP in
    all) run_B1; run_B2; run_B3 ;;
    B1)  run_B1 ;;
    B2)  run_B2 ;;
    B3)  run_B3 ;;
    *)
        echo "Error: unknown group '$EXPERIMENT_GROUP'"
        echo "Run $0 --help for available groups."
        exit 1
        ;;
esac

if [ "$FINAL_CLEANUP" = true ] && [ "$DRY_RUN" = false ]; then
    final_cleanup
fi

echo ""
echo "============================================"
echo "Godel batch experiments completed!"
echo "  Total run: $TOTAL_RUN"
echo "  Failed:    ${#FAILED_EXPERIMENTS[@]}"
if [ ${#FAILED_EXPERIMENTS[@]} -gt 0 ]; then
    echo ""
    echo "Failed experiments:"
    for exp in "${FAILED_EXPERIMENTS[@]}"; do echo "  - $exp"; done
fi
echo "============================================"
