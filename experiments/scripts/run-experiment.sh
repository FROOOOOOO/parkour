#!/bin/bash

# Automated experiment runner for Para-Sched
# Runs a single experiment: create nodes, configure scheduler, run workload, collect metrics.
# Workloads are injected via CL2 batch (burst) creation.
#
# With --trials N, runs N independent trials sharing the same nodes and scheduler config.
# Between trials, scheduler state is reset (CRD instances + binder/dispatcher restart)
# to ensure trial independence.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
RESULTS_DIR="$PROJECT_ROOT/experiments/results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Default parameters
EXPERIMENT_NAME=""
NUM_NODES=10000
NUM_SCHEDULERS=10
NUM_BACKUP=2
CONFLICT_PENALTY=0.3
STRATEGY_NAME="QualityFirst"   # QualityFirst | LatencyFirst | WeightedRandom | QualityFirstParSync | LatencyFirstParSync
STRATEGY_SEED=42                # Seed for WeightedRandom / ParSync PRNG (ignored by deterministic strategies)
SYNC_PERIOD=1.0
NUM_PARTITIONS=10
SYNC_PATTERN="diff"    # glob (P1), same (P2), diff (P3/P4)
NUM_TRIALS=1
PODS_PER_NODE=""        # CL2 default: 29
CPU_REQUEST=""          # CL2 default: 1000m
MEMORY_REQUEST=""       # CL2 default: 8Gi
VARIANCE="0"            # Capacity-variance level for HC-V experiments (0 = homogeneous HC-1)
BINDER_WORKERS=8         # Number of binder worker goroutines (default: 8; set to 1 for Godel-aligned)
# Prometheus endpoint: --prometheus-url, else PROMETHEUS_URL from the
# environment or experiments/site.env, else localhost.
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9091}"
COLLECT_LOGS=false
PRESERVE_NODES=false    # If true: skip Step 1 node create (when count AND variance match) and Step 6 node delete
REGISTRY_BOARD=""       # Registry identity of the cell this run measures, recorded in config.json
REGISTRY_CELL=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --name)
            EXPERIMENT_NAME="$2"
            shift 2
            ;;
        --results-dir)
            RESULTS_DIR="$2"
            shift 2
            ;;
        --board)
            REGISTRY_BOARD="$2"
            shift 2
            ;;
        --cell)
            REGISTRY_CELL="$2"
            shift 2
            ;;
        --nodes)
            NUM_NODES="$2"
            shift 2
            ;;
        --schedulers)
            NUM_SCHEDULERS="$2"
            shift 2
            ;;
        --backup)
            NUM_BACKUP="$2"
            shift 2
            ;;
        --penalty)
            CONFLICT_PENALTY="$2"
            shift 2
            ;;
        --strategy)
            STRATEGY_NAME="$2"
            shift 2
            ;;
        --strategy-seed)
            STRATEGY_SEED="$2"
            shift 2
            ;;
        --sync-period)
            SYNC_PERIOD="$2"
            shift 2
            ;;
        --partitions)
            NUM_PARTITIONS="$2"
            shift 2
            ;;
        --sync-pattern)
            SYNC_PATTERN="$2"
            shift 2
            ;;
        --trials)
            NUM_TRIALS="$2"
            shift 2
            ;;
        --pods-per-node)
            PODS_PER_NODE="$2"
            shift 2
            ;;
        --cpu-request)
            CPU_REQUEST="$2"
            shift 2
            ;;
        --memory-request)
            MEMORY_REQUEST="$2"
            shift 2
            ;;
        --variance)
            VARIANCE="$2"
            shift 2
            ;;
        --binder-workers)
            BINDER_WORKERS="$2"
            shift 2
            ;;
        --prometheus-url)
            PROMETHEUS_URL="$2"
            shift 2
            ;;
        --preserve-nodes)
            PRESERVE_NODES=true
            shift
            ;;
        --collect-logs)
            COLLECT_LOGS=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 --name <experiment_name> [options]"
            echo ""
            echo "Required:"
            echo "  --name NAME              Experiment name (used as result directory prefix)"
            echo ""
            echo "Scheduling method parameters:"
            echo "  --backup NUM             Backup candidates K (default: 2, 0=disable multicandidate)"
            echo "  --strategy STR           Scoring strategy: QualityFirst | LatencyFirst | WeightedRandom |"
            echo "                                              QualityFirstParSync | LatencyFirstParSync (default: QualityFirst)"
            echo "                             QualityFirst        : adjusted = (1-p)*normScore + p*(1-conflictRate)"
            echo "                             WeightedRandom      : weighted random sampling using the same formula"
            echo "                             LatencyFirst        : Freshness-first (ignores p), only meaningful with ParSync"
            echo "                             QualityFirstParSync : ParSync paper §6.1 — partition-grain quality (avg score) + within-partition weighted sampling"
            echo "                             LatencyFirstParSync : ParSync paper §6.1 — partition-grain freshness + within-partition weighted sampling"
            echo "  --penalty FLOAT          Penalty weight p (default: 0.3, 0=disable penalty; applies to all strategies except LatencyFirst)"
            echo "  --strategy-seed INT      PRNG seed for WeightedRandom / ParSync strategies (default: 42)"
            echo ""
            echo "Sync mode parameters:"
            echo "  --sync-period FLOAT      Sync period G in seconds (default: 1.0)"
            echo "  --partitions NUM         Number of partitions M (default: 10)"
            echo "  --sync-pattern STR       Partition sync pattern (default: diff)"
            echo "                             glob = all-partition global sync (P1)"
            echo "                             same = all schedulers sync same partition (P2)"
            echo "                             diff = schedulers sync different partitions (P3/P4)"
            echo ""
            echo "  Sync mode is auto-detected from --sync-period:"
            echo "    sync-period >= 0.5  =>  periodic mode (ParSync enabled)"
            echo "    sync-period <  0.5  =>  event mode (K8s informer, ParSync disabled)"
            echo "  Event mode (E1/E2/E3): use --sync-period 0.1"
            echo "  Periodic mode (P1-P4): use --sync-period 1.0 (or 0.5/2.5/5.0)"
            echo ""
            echo "Cluster and workload parameters:"
            echo "  --nodes NUM              Number of KWOK nodes (default: 10000)"
            echo "  --schedulers NUM         Number of scheduler instances (default: 10)"
            echo "  --pods-per-node NUM      Saturation pods per node (default: 29)"
            echo "  --cpu-request STR        Pod CPU request, e.g. '24000m' (default: CL2 1000m)"
            echo "  --memory-request STR     Pod memory request, e.g. '192Gi' (default: CL2 8Gi)"
            echo "  --variance V             Capacity-variance level: 0 | 0.3 | 0.6 | 1.0 (default: 0)"
            echo "                             V=0 reproduces legacy HC-1 (all shards 32 CPU / 256 Gi)"
            echo "                             V>0 yields heterogeneous shards (see generate-hetero-config.py)"
            echo "  --trials NUM             Number of independent trials (default: 1)"
            echo ""
            echo "Infrastructure:"
            echo "  --results-dir DIR        Directory the run directory is created in"
            echo "                           (default: experiments/results; batch drivers pass the board's)"
            echo "  --board BOARD --cell C   The registry cell this run measures, recorded in"
            echo "                           config.json (see experiments/registry.json)"
            echo "  --prometheus-url URL     Prometheus URL (default: $PROMETHEUS_URL, from PROMETHEUS_URL or experiments/site.env)"
            echo "  --collect-logs           Save scheduler/binder/dispatcher logs per trial"
            echo "  --preserve-nodes         Reuse existing KWOK nodes if count matches (Step 1 skips"
            echo "                           create; Step 6 skips node delete). Batch scripts pass this"
            echo "                           across same-node-count experiments to cut per-run overhead."
            echo ""
            echo "Examples:"
            echo "  # E2 baseline (event-driven, no optimization)"
            echo "  $0 --name A2-2000n-E2 --nodes 2000 --schedulers 5 \\"
            echo "     --backup 0 --penalty 0 --sync-period 0.1 \\"
            echo "     --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi --trials 3"
            echo ""
            echo "  # E3 proposed (event-driven + QualityFirst + penalty)"
            echo "  $0 --name A2-2000n-E3 --nodes 2000 --schedulers 5 \\"
            echo "     --backup 2 --strategy QualityFirst --penalty 0.3 --sync-period 0.1 \\"
            echo "     --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi --trials 3"
            echo ""
            echo "  # P4 proposed (periodic + LatencyFirst, no penalty tuning)"
            echo "  $0 --name A2-2000n-P4 --nodes 2000 --schedulers 5 \\"
            echo "     --backup 2 --strategy LatencyFirst \\"
            echo "     --sync-period 1.0 --partitions 5 --sync-pattern diff \\"
            echo "     --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi --trials 3"
            echo ""
            echo "  # WeightedRandom with fixed seed"
            echo "  $0 --name S-Strategy-WR --nodes 10000 --schedulers 10 \\"
            echo "     --backup 2 --strategy WeightedRandom --penalty 0.3 --strategy-seed 42 \\"
            echo "     --sync-period 0.1 --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi --trials 3"
            echo ""
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate required parameters
if [ -z "$EXPERIMENT_NAME" ]; then
    echo "Error: --name is required"
    exit 1
fi
if [[ -n "$REGISTRY_BOARD" && -z "$REGISTRY_CELL" ]] || [[ -z "$REGISTRY_BOARD" && -n "$REGISTRY_CELL" ]]; then
    echo "Error: --board and --cell go together"
    exit 1
fi

case "$STRATEGY_NAME" in
    QualityFirst|LatencyFirst|WeightedRandom|QualityFirstParSync|LatencyFirstParSync) ;;
    *)
        echo "Error: --strategy must be QualityFirst | LatencyFirst | WeightedRandom | QualityFirstParSync | LatencyFirstParSync (got: $STRATEGY_NAME)"
        exit 1
        ;;
esac

# Create results directory
EXPERIMENT_DIR="$RESULTS_DIR/${EXPERIMENT_NAME}_${TIMESTAMP}"
mkdir -p "$EXPERIMENT_DIR"

echo "============================================"
echo "Para-Sched Experiment Runner"
echo "============================================"
echo "Experiment: $EXPERIMENT_NAME"
echo "Results dir: $EXPERIMENT_DIR"
echo ""
# Pre-compute sync mode for banner display (same logic as Step 2)
_BANNER_SYNC_MODE="event"
if (( $(echo "$SYNC_PERIOD >= 0.5" | bc -l) )); then
    _BANNER_SYNC_MODE="periodic"
fi

echo "Parameters:"
echo "  Nodes: $NUM_NODES"
echo "  Schedulers: $NUM_SCHEDULERS"
echo "  Backup candidates (K): $NUM_BACKUP"
echo "  Strategy: $STRATEGY_NAME (seed=$STRATEGY_SEED)"
echo "  Penalty weight (p): $CONFLICT_PENALTY"
echo "  Sync period (G): ${SYNC_PERIOD}s → ${_BANNER_SYNC_MODE} mode"
if [ "$_BANNER_SYNC_MODE" = "periodic" ]; then
    echo "  Partitions (M): $NUM_PARTITIONS"
    echo "  Sync pattern: $SYNC_PATTERN"
fi
echo "  Trials: $NUM_TRIALS"
if [ -n "$PODS_PER_NODE" ]; then echo "  Pods/node: $PODS_PER_NODE"; fi
if [ -n "$CPU_REQUEST" ]; then echo "  CPU request: $CPU_REQUEST"; fi
if [ -n "$MEMORY_REQUEST" ]; then echo "  Memory request: $MEMORY_REQUEST"; fi
echo "  Capacity variance (V): $VARIANCE"
if [ "$COLLECT_LOGS" = "true" ]; then echo "  Collect logs: yes"; fi
echo "  Prometheus: $PROMETHEUS_URL"
if [ "$COLLECT_LOGS" = true ]; then echo "  Collect logs: ENABLED"; fi
echo "============================================"
echo ""

# Save experiment configuration. A run launched for a registry cell records
# which one, so its identity does not rest on its name alone.
REGISTRY_FIELDS=""
if [ -n "$REGISTRY_CELL" ]; then
    REGISTRY_FIELDS="  \"board\": \"$REGISTRY_BOARD\",
  \"cell\": \"$REGISTRY_CELL\",
"
fi
cat > "$EXPERIMENT_DIR/config.json" <<EOF
{
  "name": "$EXPERIMENT_NAME",
${REGISTRY_FIELDS}  "timestamp": "$TIMESTAMP",
  "num_trials": $NUM_TRIALS,
  "parameters": {
    "num_nodes": $NUM_NODES,
    "num_schedulers": $NUM_SCHEDULERS,
    "num_backup": $NUM_BACKUP,
    "strategy": "$STRATEGY_NAME",
    "strategy_seed": $STRATEGY_SEED,
    "conflict_penalty": $CONFLICT_PENALTY,
    "sync_period": $SYNC_PERIOD,
    "num_partitions": $NUM_PARTITIONS,
    "sync_pattern": "$SYNC_PATTERN",
    "pods_per_node": "${PODS_PER_NODE:-29}",
    "cpu_request": "${CPU_REQUEST:-1000m}",
    "memory_request": "${MEMORY_REQUEST:-8Gi}",
    "capacity_variance": "$VARIANCE"
  }
}
EOF

# ============================================================
# Shared variables
# ============================================================
DEPLOY_DIR="$PROJECT_ROOT/para-scheduler/deploy/lab-cluster"
NAMESPACE="para-system"
CL2_BIN="$PROJECT_ROOT/bin/clusterloader"
CONFIG_DIR="$PROJECT_ROOT/experiments/kwok-setup"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/config}"

# ============================================================
# Helper: unix timestamp (whole seconds) of a CL2 log step marker, or an empty
# line when the log has none.
# CL2 logs: I0408 16:20:07.745190 ... Step "[step: 03] <name>" started/ended
# experiments/common/cl2.py is the one CL2 log parser: the reduction reads the
# same markers to time each phase, so both agree on the window.
# Usage: parse_cl2_ts <log_file> <step_name> <started|ended>
# ============================================================
parse_cl2_ts() {
    python3 "$PROJECT_ROOT/experiments/common/cl2.py" step-time "$1" "$2" "$3" || echo ""
}

# ============================================================
# Helper: collect metrics for a specific phase (saturation or latency)
# Usage: collect_phase_metrics <trial_dir> <phase_name> <start_ts> <end_ts>
# ============================================================
collect_phase_metrics() {
    local trial_dir=$1 phase=$2 start=$3 end=$4
    if [ -z "$start" ] || [ -z "$end" ] || [ "$start" -ge "$end" ]; then
        echo "    WARN: could not parse ${phase} phase timestamps, skipping"
        return
    fi
    echo "    ${phase}: $(date -d @${start} +%H:%M:%S) → $(date -d @${end} +%H:%M:%S) ($((end - start))s)"
    # --snap-end-pad 30: extend snapshot (counter/histogram diff) end by 30s so
    # Prometheus captures >= 2 extra scrapes after the saturation phase, giving
    # histograms enough samples for percentile computation in short trials
    # (e.g. A2-2000n whose scheduling duration was ~50s).  Range/instant queries
    # still use the unpadded end, preserving resource-metric semantics.
    "$SCRIPT_DIR/collect-metrics.sh" \
        --output "$trial_dir/metrics-${phase}" \
        --start "$start" \
        --end "$end" \
        --snap-end-pad 30 \
        --prometheus-url "$PROMETHEUS_URL" \
        || echo "    WARN: metrics collection failed for ${phase} phase (non-fatal)"
}

# ============================================================
# Helper: wait for all para-sched components to be ready
# ============================================================
wait_for_components() {
    echo "  Waiting for all components to be ready..."

    # Sanity check: ensure no excess pods linger from a previous experiment.
    # This guards against the case where Step 2's pod termination was slower
    # than expected.  Check ALL component types — not just schedulers — to
    # prevent Prometheus from scraping stale binder/dispatcher pods and
    # inflating resource usage metrics (scheduler_instances mismatch issue).
    wait_for_exact_pod_count() {
        local label=$1 expected=$2 component=$3
        local live
        live=$(kubectl -n "$NAMESPACE" get pods -l "app=$label" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
        if [ "$live" -gt "$expected" ]; then
            echo "  WARNING: $live $component pods running (expected $expected), waiting for excess to terminate..."
            local deadline=$(($(date +%s) + 60))
            while [ "$(kubectl -n "$NAMESPACE" get pods -l "app=$label" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)" -gt "$expected" ]; do
                if [ "$(date +%s)" -ge "$deadline" ]; then
                    echo "  WARNING: excess $component pods still present after 60s, proceeding."
                    break
                fi
                sleep 3
            done
        fi
    }

    wait_for_exact_pod_count "para-scheduler" "$NUM_SCHEDULERS" "scheduler"
    wait_for_exact_pod_count "para-binder"    "1"               "binder"
    wait_for_exact_pod_count "para-dispatcher" "1"              "dispatcher"

    kubectl -n "$NAMESPACE" rollout status deployment/para-binder --timeout=120s
    kubectl -n "$NAMESPACE" rollout status deployment/para-dispatcher --timeout=120s
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        kubectl -n "$NAMESPACE" rollout status deployment/para-scheduler-${i} --timeout=120s
    done
    echo "  All components ready."
}

# ============================================================
# Helper: reset scheduler state between trials
# Deletes CRD instances + restarts Binder/Dispatcher to clear
# in-memory conflict stats, partition manager, dispatcher queues.
# Does NOT delete KWOK nodes (they are shared across trials).
# ============================================================
reset_scheduler_state() {
    echo "  Resetting state for next trial..."

    # 1. Ensure CL2 test pods are fully removed (fix B: stronger cleanup).
    #    CL2 deletes pods internally (replicas=0 + gather), but on KWOK nodes the
    #    deletion depends on KWOK updating pod status.  If CL2 timed out, pods may
    #    still be terminating.  Force-delete and poll to be sure.
    #
    #    Background on raised timeout: HC-1 (1 pod/node) experiments submit 10k pods;
    #    60s was too tight — apiserver GC batch ≈100-200 pods/s.  Residual Terminating
    #    pods still holding .spec.nodeName will poison the next trial's Binder informer
    #    cache (every node looks occupied → bind_conflict_rate = 1.0, 0 bind success).
    #    Root cause diagnosed from S-E-K2 trial-2 data.
    echo "    Force-deleting remaining test pods..."
    CL2_TRIAL_NS=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^test-' || true)
    for ns in $CL2_TRIAL_NS; do
        kubectl delete pods --all -n "$ns" --force --grace-period=0 --ignore-not-found=true 2>/dev/null &
    done
    wait
    # Poll until test pods are gone (timeout raised 60s → 180s for HC-1 10k-pod scale).
    # On final timeout, iterate and force-delete each remaining pod by name as last resort.
    TRIAL_POD_DEADLINE=$(($(date +%s) + 180))
    while true; do
        REMAINING=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
        [ "$REMAINING" -eq 0 ] && break
        if [ "$(date +%s)" -ge "$TRIAL_POD_DEADLINE" ]; then
            echo "    Pod cleanup timeout with $REMAINING left — force-deleting by name..."
            kubectl get pods --all-namespaces -l 'group in (saturation, latency)' \
                -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' 2>/dev/null \
                | while read ns pname; do
                    [ -n "$ns" ] && [ -n "$pname" ] || continue
                    kubectl -n "$ns" delete pod "$pname" --force --grace-period=0 --ignore-not-found=true 2>/dev/null || true
                done
            sleep 5
            FINAL_REMAINING=$(kubectl get pods --all-namespaces -l 'group in (saturation, latency)' --no-headers 2>/dev/null | wc -l)
            if [ "$FINAL_REMAINING" -gt 0 ]; then
                echo "    WARNING: $FINAL_REMAINING pods still present after force-delete; proceeding anyway."
            fi
            break
        fi
        sleep 3
    done

    # 2. Delete CL2 test namespaces (async, but wait briefly for completion).
    echo "    Cleaning CL2 test namespaces..."
    for ns in $CL2_TRIAL_NS; do
        kubectl delete ns "$ns" --ignore-not-found=true --wait=false 2>/dev/null || true
    done
    TRIAL_NS_DEADLINE=$(($(date +%s) + 60))
    while true; do
        TERMINATING=$(kubectl get ns --field-selector=status.phase=Terminating -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
            | tr ' ' '\n' | grep '^test-' || true)
        [ -z "$TERMINATING" ] && break
        if [ "$(date +%s)" -ge "$TRIAL_NS_DEADLINE" ]; then
            echo "    Namespace cleanup timeout, force-finalizing..."
            for ns in $TERMINATING; do
                kubectl get ns "$ns" -o json 2>/dev/null \
                    | jq '.spec.finalizers = []' \
                    | kubectl replace --raw "/api/v1/namespaces/$ns/finalize" -f - 2>/dev/null || true
            done
            sleep 3
            break
        fi
        sleep 3
    done

    # 3. Scale ALL components to 0 (fix A: clean restart, not rolling).
    #    Previously we used `rollout restart` for scheduler/dispatcher which is rolling
    #    (new pod Ready before old pod terminates). Symptoms observed in S-E-K2 trial-2:
    #      - Prometheus scrapes both old and new series during the overlap window
    #        → scheduler_instances = 2×N, scheduler_cpu/mem_rss doubled
    #      - On rare occasions the new scheduler/binder pod begins serving before the
    #        old pod is gone, inheriting an informer connection that still has
    #        residual pod events in flight from the previous trial
    #    Scale-to-0 → wait-for-delete → scale-to-1 enforces a hard restart boundary.
    echo "    Scaling all components to 0 for clean restart..."
    kubectl -n "$NAMESPACE" scale deployment/para-binder --replicas=0 --timeout=60s 2>/dev/null || true
    kubectl -n "$NAMESPACE" scale deployment/para-dispatcher --replicas=0 --timeout=60s 2>/dev/null || true
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        kubectl -n "$NAMESPACE" scale deployment/para-scheduler-${i} --replicas=0 --timeout=60s 2>/dev/null || true
    done
    # Wait for all pods to be fully terminated before touching CRDs/ConfigMaps.
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=para-binder     --timeout=90s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=para-dispatcher  --timeout=90s 2>/dev/null || true
    kubectl -n "$NAMESPACE" wait --for=delete pod -l app=para-scheduler   --timeout=90s 2>/dev/null || true

    # 4. Delete CRD instances and snapshot ConfigMaps (all components stopped, safe).
    echo "    Cleaning CRDs and snapshot ConfigMaps..."
    kubectl delete schedulerassignments.scheduling.parscheduler.io --all --ignore-not-found 2>/dev/null || true
    kubectl delete parsyncconfigs.scheduling.parscheduler.io --all --ignore-not-found 2>/dev/null || true
    kubectl delete adoptionstats.scheduling.parscheduler.io --all --ignore-not-found 2>/dev/null || true
    kubectl -n "$NAMESPACE" delete configmap -l app=parasched-snapshot --ignore-not-found 2>/dev/null || true
    for i in $(seq 0 $((NUM_PARTITIONS - 1))); do
        kubectl -n "$NAMESPACE" delete configmap "parasched-snapshot-${i}" --ignore-not-found 2>/dev/null || true
    done

    # 5. Scale all components back to 1 (fresh pods, fresh informer caches).
    echo "    Scaling all components back up..."
    kubectl -n "$NAMESPACE" scale deployment/para-binder     --replicas=1 2>/dev/null || true
    kubectl -n "$NAMESPACE" scale deployment/para-dispatcher --replicas=1 2>/dev/null || true
    for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
        kubectl -n "$NAMESPACE" scale deployment/para-scheduler-${i} --replicas=1 2>/dev/null || true
    done

    # 6. Re-create AdoptionStats CR (Binder needs it)
    kubectl apply -f "$DEPLOY_DIR/adoption-stats.yaml" 2>/dev/null || true

    # 7. Brief API settle check — wait for etcd to drain the pod/namespace GC backlog.
    #    Shorter timeout than final cleanup Step 6 (60s vs 300s) since the scale is much smaller
    #    (only one trial's worth of pods, not a full experiment + node deletion).
    echo "    Waiting for API server to settle..."
    settle_briefly 60 "    "
}

# ============================================================
# Step 1: Create KWOK nodes (once for all trials)
# ============================================================

# Step 1a: Restart KWOK shards IMMEDIATELY before node creation.
# KWOK uses API Server Watch to detect new nodes matching its annotation filter.
# If the shard was restarted much earlier (e.g., during the cleanup phase of the
# previous experiment), the Watch connection may experience delays or event loss
# during large-scale node creation (10 shards x 1000+ nodes), causing some shards
# to discover their nodes minutes late.  Late discovery means no lease renewals →
# node-lifecycle-controller marks them NotReady after 40s grace period.
# Restarting right before creation ensures a fresh Watch connection.
echo "Step 1a: Restarting KWOK shards (fresh Watch connection)..."
sudo systemctl restart kwok kwok{1..9} 2>/dev/null || true
sleep 3

# Step 1b: Node provisioning.
# With --preserve-nodes, reuse existing KWOK nodes when BOTH count AND variance match
# the target. Either mismatch → purge and recreate. Without --preserve-nodes (default),
# always create fresh.
#
# Variance detection: kwok-node.yaml stamps the label parasched.io/capacity-variance=<V>
# at creation. We read any one KWOK node's label; if absent (legacy node from a pre-HC-V
# run) we treat it as "0". This lets HC-V sweeps swap between V levels while still
# sharing nodes across same-V experiments.
EXISTING_KWOK=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l)
EXISTING_VARIANCE=""
if [ "$EXISTING_KWOK" -gt 0 ]; then
    EXISTING_VARIANCE=$(kubectl get nodes -l type=kwok \
        -o jsonpath='{.items[0].metadata.labels.parasched\.io/capacity-variance}' 2>/dev/null)
    [ -z "$EXISTING_VARIANCE" ] && EXISTING_VARIANCE="0"
fi

NEED_RECREATE=false
if [ "$PRESERVE_NODES" = true ] && [ "$EXISTING_KWOK" = "$NUM_NODES" ] && [ "$EXISTING_VARIANCE" = "$VARIANCE" ]; then
    echo "Step 1b: Preserving $EXISTING_KWOK existing KWOK nodes (count=$NUM_NODES, V=$VARIANCE match)."
elif [ "$PRESERVE_NODES" = true ] && [ "$EXISTING_KWOK" -gt 0 ]; then
    if [ "$EXISTING_KWOK" != "$NUM_NODES" ]; then
        echo "Step 1b: Node count mismatch (existing=$EXISTING_KWOK, target=$NUM_NODES) — purging and recreating..."
    else
        echo "Step 1b: Variance mismatch (existing V=$EXISTING_VARIANCE, target V=$VARIANCE) — purging and recreating..."
    fi
    NEED_RECREATE=true
else
    NEED_RECREATE=true
fi

if [ "$NEED_RECREATE" = true ]; then
    if [ "$EXISTING_KWOK" -gt 0 ]; then
        kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
    fi
    echo "Step 1b: Creating $NUM_NODES KWOK nodes (V=$VARIANCE)..."
    "$SCRIPT_DIR/create-nodes.sh" --nodes "$NUM_NODES" --variance "$VARIANCE"
fi

# Step 1c: Wait for ALL KWOK nodes to become Ready.
# CL2 create-nodes only waits for the Node objects to exist, not for KWOK to
# patch their status to Ready.  Each KWOK shard must: (1) detect the node via
# Watch, (2) create a Lease in kube-node-lease, (3) patch node status.
# If any shard is slow, those nodes stay NotReady and are unschedulable.
echo "Step 1c: Waiting for all $NUM_NODES KWOK nodes to be Ready..."
NODE_READY_TIMEOUT=300
NODE_READY_START=$(date +%s)
while true; do
    TOTAL_KWOK=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l)
    READY_KWOK=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | grep -c ' Ready' || true)
    NOT_READY=$((TOTAL_KWOK - READY_KWOK))

    if [ "$READY_KWOK" -ge "$NUM_NODES" ]; then
        echo "  All $READY_KWOK/$TOTAL_KWOK KWOK nodes are Ready."
        break
    fi

    ELAPSED=$(( $(date +%s) - NODE_READY_START ))
    if [ "$ELAPSED" -ge "$NODE_READY_TIMEOUT" ]; then
        echo "  WARNING: Node readiness timeout (${NODE_READY_TIMEOUT}s)."
        echo "  $READY_KWOK Ready, $NOT_READY NotReady out of $TOTAL_KWOK total."
        # Identify which shards have NotReady nodes
        echo "  NotReady nodes per shard:"
        for shard in $(seq 0 9); do
            NR=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null \
                | grep "kwok-s${shard}-" | grep -cv ' Ready' || true)
            if [ "$NR" -gt 0 ]; then
                echo "    kwok-s${shard}: $NR NotReady"
            fi
        done
        echo "  Attempting targeted KWOK shard restart for NotReady shards..."
        for shard in $(seq 0 9); do
            NR=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null \
                | grep "kwok-s${shard}-" | grep -cv ' Ready' || true)
            if [ "$NR" -gt 0 ]; then
                SVC="kwok"
                [ "$shard" -gt 0 ] && SVC="kwok${shard}"
                echo "    Restarting $SVC ($NR NotReady nodes)..."
                sudo systemctl restart "$SVC" 2>/dev/null || true
            fi
        done
        # Wait additional time after targeted restart
        echo "  Waiting 60s for targeted restart to take effect..."
        sleep 60
        READY_KWOK=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | grep -c ' Ready' || true)
        echo "  After restart: $READY_KWOK/$TOTAL_KWOK Ready."
        break
    fi

    echo "  Nodes Ready: $READY_KWOK/$TOTAL_KWOK ($NOT_READY NotReady, ${ELAPSED}s/${NODE_READY_TIMEOUT}s)"
    sleep 10
done

# ============================================================
# Step 2: Configure scheduler parameters (once for all trials)
# ============================================================
echo ""
echo "Step 2: Configuring scheduler parameters..."

# Ensure AdoptionStats CR exists (Binder needs it for stats reporting)
kubectl apply -f "$DEPLOY_DIR/adoption-stats.yaml" 2>/dev/null

# Determine sync mode: periodic if sync_period >= 0.5
SYNC_MODE="event"
if (( $(echo "$SYNC_PERIOD >= 0.5" | bc -l) )); then
    SYNC_MODE="periodic"
fi

# Determine feature flags from experiment parameters
# Phase 2: --parasched-strategy.{name,penalty-weight,seed} replaces the old enable-penalty + penalty-weight flags.
ENABLE_PARSYNC="false"
if [ "$SYNC_MODE" = "periodic" ]; then
    ENABLE_PARSYNC="true"
fi

# Script-layer double safety:
# glob is the P=1 special case of periodic sync. Force EFFECTIVE_PARTITIONS=1
# so the CLI args of dispatcher/scheduler/binder are correct even if the
# ParSyncConfig CRD read path fails. The Dispatcher also forces P=1 at CRD
# write (para-scheduler/pkg/dispatcher/partition.go:47-49); this is redundant
# but cheap and avoids glob snapshot oversize bug recurring on any CRD read
# failure.
EFFECTIVE_PARTITIONS="$NUM_PARTITIONS"
if [ "$SYNC_PATTERN" = "glob" ] && [ "$SYNC_MODE" = "periodic" ]; then
    EFFECTIVE_PARTITIONS=1
fi

echo "  Sync mode: $SYNC_MODE"
echo "  Scheduler strategy: $STRATEGY_NAME (p=$CONFLICT_PENALTY, seed=$STRATEGY_SEED)"
echo "  Enable parsync: $ENABLE_PARSYNC"
if [ "$SYNC_MODE" = "periodic" ] && [ "$EFFECTIVE_PARTITIONS" != "$NUM_PARTITIONS" ]; then
    echo "  Effective partitions (glob coercion): $EFFECTIVE_PARTITIONS"
fi

# 2-pre. Strip stale ParSync partition labels from KWOK nodes.
# When --preserve-nodes reuses nodes across experiments, periodic-mode labels from
# the previous run can leak. Dispatcher only re-labels when it owns assignment, so
# pre-existing labels may survive into the next experiment's partitioning plan.
# Strip unconditionally so Dispatcher (in periodic mode) always starts from zero.
# Cheap op — a no-op if no labels exist.
echo "  Stripping stale partition-id labels from KWOK nodes..."
kubectl label nodes -l type=kwok para-scheduler.io/partition-id- --overwrite 2>/dev/null | tail -1 || true

# 2a. Scale scheduler count if needed (must happen BEFORE patching args,
#     otherwise kubectl patch fails on non-existent deployments and set -e kills the script)
CURRENT_SCHED_COUNT=$(kubectl -n "$NAMESPACE" get deployments -l app=para-scheduler --no-headers 2>/dev/null | wc -l)
echo "  Current schedulers: $CURRENT_SCHED_COUNT, target: $NUM_SCHEDULERS"

if [ "$CURRENT_SCHED_COUNT" -gt "$NUM_SCHEDULERS" ]; then
    echo "  Removing excess schedulers ($CURRENT_SCHED_COUNT → $NUM_SCHEDULERS)..."
    for i in $(seq "$NUM_SCHEDULERS" $((CURRENT_SCHED_COUNT - 1))); do
        kubectl -n "$NAMESPACE" delete deployment "para-scheduler-${i}" --ignore-not-found --wait=true --timeout=60s 2>/dev/null || true
        kubectl -n "$NAMESPACE" delete service "scheduler-metrics-${i}" --ignore-not-found 2>/dev/null || true
    done
    # Wait until excess scheduler pods are fully terminated.
    # deployment --wait=true only waits for the API object deletion; the pods may
    # still be in graceful shutdown.  Poll until no excess pods remain.
    echo "  Waiting for excess scheduler pods to terminate..."
    EXCESS_WAIT_TIMEOUT=60
    EXCESS_WAIT_START=$(date +%s)
    while true; do
        LIVE_SCHED=$(kubectl -n "$NAMESPACE" get pods -l app=para-scheduler --no-headers 2>/dev/null | wc -l)
        if [ "$LIVE_SCHED" -le "$NUM_SCHEDULERS" ]; then
            echo "  Excess scheduler pods terminated ($LIVE_SCHED remaining)."
            break
        fi
        ELAPSED=$(( $(date +%s) - EXCESS_WAIT_START ))
        if [ "$ELAPSED" -ge "$EXCESS_WAIT_TIMEOUT" ]; then
            echo "  WARNING: $LIVE_SCHED scheduler pods still running after ${EXCESS_WAIT_TIMEOUT}s, proceeding."
            break
        fi
        echo "  Waiting for excess pods to terminate... ($LIVE_SCHED pods, ${ELAPSED}s/${EXCESS_WAIT_TIMEOUT}s)"
        sleep 3
    done
fi

if [ "$CURRENT_SCHED_COUNT" -lt "$NUM_SCHEDULERS" ]; then
    echo "  Creating missing schedulers..."
    for i in $(seq "$CURRENT_SCHED_COUNT" $((NUM_SCHEDULERS - 1))); do
        SCHED_INDEX="$i" envsubst '$SCHED_INDEX' \
            < "$DEPLOY_DIR/scheduler-template.yaml" | kubectl apply -f - 2>/dev/null
        SCHED_INDEX="$i" SCHED_NODE_PORT=$((30090 + i)) \
            envsubst '$SCHED_INDEX $SCHED_NODE_PORT' \
            < "$DEPLOY_DIR/scheduler-metrics-svc-template.yaml" | kubectl apply -f - 2>/dev/null
    done
fi

# 2b. Patch args on all scheduler deployments (all deployments now exist)
echo "  Patching $NUM_SCHEDULERS scheduler deployments..."
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    SCHED_ARGS="[\"--config=/etc/scheduler/scheduler-config.yaml\",\"--v=3\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-name=sched-${i}\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-candidate-k=$NUM_BACKUP\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-strategy.name=$STRATEGY_NAME\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-strategy.penalty-weight=$CONFLICT_PENALTY\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-strategy.seed=$STRATEGY_SEED\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-enable-parsync=$ENABLE_PARSYNC\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-num-partitions=$EFFECTIVE_PARTITIONS\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-sync-period=${SYNC_PERIOD}s\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-namespace=para-system\""
    SCHED_ARGS="$SCHED_ARGS,\"--parasched-stats-name=default\""
    SCHED_ARGS="$SCHED_ARGS]"
    kubectl -n "$NAMESPACE" patch deployment para-scheduler-${i} --type=json \
        -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$SCHED_ARGS}]" \
        2>/dev/null
done

# 2c. Update Dispatcher args (scheduler names + sync mode)
SCHED_NAMES=""
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    [ -n "$SCHED_NAMES" ] && SCHED_NAMES="$SCHED_NAMES,"
    SCHED_NAMES="${SCHED_NAMES}sched-${i}"
done
DISPATCHER_ARGS="[\"--scheduler-names=$SCHED_NAMES\",\"--sync-mode=$SYNC_MODE\",\"--workers=4\",\"--metrics-addr=:8081\",\"--readiness-addr=:8082\",\"--kube-api-qps=10000\",\"--kube-api-burst=10000\""
if [ "$SYNC_MODE" = "periodic" ]; then
    DISPATCHER_ARGS="$DISPATCHER_ARGS,\"--sync-pattern=$SYNC_PATTERN\",\"--sync-period=${SYNC_PERIOD}s\",\"--num-partitions=$EFFECTIVE_PARTITIONS\",\"--expected-nodes=$NUM_NODES\""
fi
DISPATCHER_ARGS="$DISPATCHER_ARGS]"
kubectl -n "$NAMESPACE" patch deployment para-dispatcher --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$DISPATCHER_ARGS}]" \
    2>/dev/null

# 2d. Update Binder args (sync mode + partitions)
BINDER_ARGS="[\"--sync-mode=$SYNC_MODE\",\"--workers=$BINDER_WORKERS\",\"--assumed-pod-ttl=30s\",\"--stats-name=default\",\"--stats-flush-period=1s\",\"--metrics-addr=:8080\",\"--kube-api-qps=10000\",\"--kube-api-burst=10000\""
if [ "$SYNC_MODE" = "periodic" ]; then
    BINDER_ARGS="$BINDER_ARGS,\"--num-partitions=$EFFECTIVE_PARTITIONS\",\"--sync-period=${SYNC_PERIOD}s\",\"--snapshot-flush-interval=100ms\""
fi
BINDER_ARGS="$BINDER_ARGS]"
kubectl -n "$NAMESPACE" patch deployment para-binder --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$BINDER_ARGS}]" \
    2>/dev/null

# 2e. Stop old Binder FIRST to prevent stale snapshot ConfigMap recreation.
# The old Binder publishes snapshots on a 1s heartbeat. If we delete ConfigMaps
# while the old Binder is still running (as the previous Step 2e/2f did), it
# recreates them with stale data from the previous experiment — causing Trial 1
# schedulers to see all nodes as fully utilized → 100% unschedulable.
# Fix: scale Binder to 0 and wait for pod termination before touching ConfigMaps.
echo "  Stopping old Binder to prevent stale snapshot recreation..."
kubectl -n "$NAMESPACE" scale deployment/para-binder --replicas=0 --timeout=60s 2>/dev/null || true
kubectl -n "$NAMESPACE" wait --for=delete pod -l app=para-binder --timeout=60s 2>/dev/null || true

# 2f. Clean stale ParSync snapshot ConfigMaps from previous experiment.
# Now safe — old Binder is stopped and cannot recreate them.
echo "  Cleaning stale ParSync snapshot ConfigMaps..."
kubectl -n "$NAMESPACE" delete configmap -l app=parasched-snapshot --ignore-not-found 2>/dev/null || true
# Belt-and-suspenders: also delete any snapshot ConfigMaps by name pattern
# (covers both legacy single-ConfigMap name `parasched-snapshot-<partition>`
# and chunked names `parasched-snapshot-<partition>-c<chunk>` — matched via
# label deletion above but enumerated here for clarity against old Binder
# versions that may not have emitted labels reliably).
for i in $(seq 0 $((EFFECTIVE_PARTITIONS - 1))); do
    kubectl -n "$NAMESPACE" delete configmap "parasched-snapshot-${i}" --ignore-not-found 2>/dev/null || true
done

# 2g. Force restart all components to guarantee clean internal state.
# kubectl patch only triggers a rolling restart when the template actually changes.
# When consecutive experiments share identical scheduler/binder args (e.g. P2 same→P3 diff
# only changes the Dispatcher's --sync-pattern), the scheduler pod is NOT restarted,
# carrying over stale cache/snapshot state from the previous experiment → all pods
# become unschedulable.  An explicit rollout restart fixes this unconditionally.
# Binder is scaled back to 1 (was set to 0 in Step 2e).
echo "  Restarting all components with clean state..."
kubectl -n "$NAMESPACE" scale deployment/para-binder --replicas=1 2>/dev/null || true
kubectl -n "$NAMESPACE" rollout restart deployment/para-binder 2>/dev/null
kubectl -n "$NAMESPACE" rollout restart deployment/para-dispatcher 2>/dev/null
for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
    kubectl -n "$NAMESPACE" rollout restart deployment/para-scheduler-${i} 2>/dev/null
done

# ============================================================
# Trial loop: Steps 3-6 repeat for each trial
# ============================================================
TOTAL_START_TS=$(date +%s)

for TRIAL in $(seq 1 "$NUM_TRIALS"); do
    if [ "$NUM_TRIALS" -gt 1 ]; then
        echo ""
        echo "============================================"
        echo "Trial $TRIAL/$NUM_TRIALS"
        echo "============================================"
    fi

    TRIAL_DIR="$EXPERIMENT_DIR/trial-${TRIAL}"
    mkdir -p "$TRIAL_DIR"

    # Step 3: Wait for all components to be ready
    echo ""
    echo "Step 3: Waiting for components..."
    wait_for_components

    # Step 3a: [ParSync only] Wait for Dispatcher to finish labeling all nodes
    # with para-scheduler.io/partition-id before starting the experiment.
    # Reason: Scheduler's freshness bonus applies only to nodes whose PartitionID
    # >= 0; unlabeled nodes degrade ParSync to non-ParSync silently. Dispatcher
    # labels nodes serially (~10-20ms/node), so at 5000+
    # nodes the initial labeling window can exceed 1 min. Measuring before
    # labeling is complete produces a systematic bias in HC-1 P-group results.
    #
    # Skipped in event-driven mode (SYNC_PERIOD < 0.5) — labels are not required.
    # Timeout 300s (5 min), the labeling hard cap.
    if (( $(echo "$SYNC_PERIOD >= 0.5" | bc -l) )); then
        # Belt-and-suspenders: kubectl wait on the dispatcher's readinessProbe
        # (backed by /ready, which checks ParSyncConfig CRD + SchedulerAssignments
        # + expected-nodes label coverage).
        # Falls through to the inline label poll below as a safety net for
        # operators running older dispatcher images without the probe.
        echo "  Step 3a-pre: Waiting for Dispatcher Ready condition (readinessProbe)..."
        kubectl -n "$NAMESPACE" wait --for=condition=ready \
            pod -l app=para-dispatcher --timeout=300s 2>/dev/null || \
            echo "  (readinessProbe wait timed out or pod has no probe; falling through to label poll)"

        echo "  Step 3a: Waiting for all nodes to be labeled (ParSync warm-up)..."
        LABEL_DEADLINE=$(($(date +%s) + 300))
        while true; do
            UNLABELED=$(kubectl get nodes -l '!para-scheduler.io/partition-id' --no-headers 2>/dev/null | wc -l)
            if [ "$UNLABELED" -eq 0 ]; then
                echo "  All nodes labeled."
                break
            fi
            if [ "$(date +%s)" -ge "$LABEL_DEADLINE" ]; then
                echo "  WARNING: label warm-up timeout — ${UNLABELED} nodes still unlabeled, proceeding anyway"
                break
            fi
            echo "    ${UNLABELED} nodes still unlabeled, waiting..."
            sleep 5
        done
    fi

    # Step 3b: API settle check — ensure etcd has drained any GC backlog from
    # the previous experiment/trial.  reset_scheduler_state() (between trials)
    # already does this, but Trial 1 was missing it, causing P-group first-trial
    # anomalies at large scale where previous experiment cleanup generates heavy
    # etcd write load.
    echo "  Waiting for API server to settle..."
    settle_briefly 60

    # Record trial start timestamp
    START_TS=$(date +%s)

    # Step 4: Run CL2
    echo ""

    # Write testoverrides to a temp file (avoid process substitution + pipe compatibility issues)
    CL2_OVERRIDES_FILE=$(mktemp /tmp/cl2-overrides-XXXXXX.yaml)
    local_ppn="${PODS_PER_NODE:-29}"
    # Always write PODS_PER_NODE so CL2 always has a valid overrides file
    echo "PODS_PER_NODE: ${local_ppn}" > "$CL2_OVERRIDES_FILE"
    if [ -n "$CPU_REQUEST" ]; then echo "SATURATION_POD_CPU: \"${CPU_REQUEST}\"" >> "$CL2_OVERRIDES_FILE"; fi
    if [ -n "$MEMORY_REQUEST" ]; then echo "SATURATION_POD_MEMORY: \"${MEMORY_REQUEST}\"" >> "$CL2_OVERRIDES_FILE"; fi
    # Set expected throughput based on pods/node for soft timeout calculation.
    #   HC-1 (ppn=1): 10 pods/s — extreme contention with high conflict rate
    #   HC (ppn<=5):  20 pods/s — high contention
    #   Low (default): 100 pods/s
    if [ "$local_ppn" -le 1 ]; then
        echo "DENSITY_TEST_THROUGHPUT: 10" >> "$CL2_OVERRIDES_FILE"
    elif [ "$local_ppn" -le 5 ]; then
        echo "DENSITY_TEST_THROUGHPUT: 20" >> "$CL2_OVERRIDES_FILE"
    fi

    if [ "$local_ppn" -le 1 ]; then
        # HC-1 (pods_per_node=1): use saturation-only CL2 config with burst injection.
        # No latency phase — nodes are fully saturated with 1 pod each.
        echo "Step 4: Scheduling pods (HC-1 saturation-only mode)..."
        CL2_CONFIG="$CONFIG_DIR/cl2-saturation-only.yaml"
    else
        echo "Step 4: Scheduling pods (batch mode)..."
        CL2_CONFIG="$CONFIG_DIR/cl2-schedule-pods.yaml"
    fi

    # Run CL2 — capture exit code so test failures (e.g. latency threshold violations)
    # don't abort the script before cleanup runs.  CL2 failures are expected in some
    # configurations (e.g. single scheduler at large scale exceeds latency SLO).
    set +o errexit
    set -o pipefail
    "$CL2_BIN" \
        --testconfig="$CL2_CONFIG" \
        --provider=local \
        --provider-configs=ROOT_KUBECONFIG="$KUBECONFIG_PATH" \
        --kubeconfig="$KUBECONFIG_PATH" \
        --v=2 \
        --enable-exec-service=false \
        --enable-prometheus-server=false \
        --nodes="$NUM_NODES" \
        --testoverrides="$CL2_OVERRIDES_FILE" \
        2>&1 | tee "$TRIAL_DIR/cl2.log"
    CL2_EXIT=$?
    set +o pipefail
    set -o errexit

    if [ "$CL2_EXIT" -ne 0 ]; then
        echo "  WARNING: CL2 exited with code $CL2_EXIT (test failures detected, continuing with cleanup)"
    fi

    rm -f "$CL2_OVERRIDES_FILE"

    # Copy CL2-generated junit.xml to trial results directory
    if [ -f "junit.xml" ]; then
        cp junit.xml "$TRIAL_DIR/junit.xml"
        rm -f junit.xml
    fi

    # Record trial end timestamp
    END_TS=$(date +%s)

    # Step 5: Parse CL2 log for phase timestamps, then collect metrics per phase
    # CL2 step names (cl2-schedule-pods.yaml and cl2-saturation-only.yaml):
    #   "Creating saturation pods"                    → saturation start
    #   "Waiting for saturation pods to be running"   → saturation end (gather)
    #   "Creating latency pods"                       → latency start (absent in HC-1 saturation-only)
    #   "Waiting for latency pods to be running"      → latency end   (absent in HC-1 saturation-only)
    echo ""
    echo "Step 5: Collecting metrics (per-phase)..."
    CL2_LOG="$TRIAL_DIR/cl2.log"

    SAT_START=$(parse_cl2_ts "$CL2_LOG" "Creating saturation pods" "started")
    SAT_END=$(parse_cl2_ts "$CL2_LOG" "Waiting for saturation pods to be running" "ended")
    LAT_START=$(parse_cl2_ts "$CL2_LOG" "Creating latency pods" "started")
    LAT_END=$(parse_cl2_ts "$CL2_LOG" "Waiting for latency pods to be running" "ended")

    collect_phase_metrics "$TRIAL_DIR" "saturation" "$SAT_START" "$SAT_END"
    collect_phase_metrics "$TRIAL_DIR" "latency" "$LAT_START" "$LAT_END"

    # Collect component logs (--collect-logs).
    # Captures scheduler/binder/dispatcher logs covering the trial window.
    # Uses --since-time/kubectl logs to fetch only the trial's time range,
    # keeping file sizes manageable even for long experiments.
    if [ "$COLLECT_LOGS" = true ]; then
        echo "  Collecting component logs..."
        LOGS_DIR="$TRIAL_DIR/logs"
        mkdir -p "$LOGS_DIR"
        SINCE_TIME=$(date -u -d "@$START_TS" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$START_TS" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
        for i in $(seq 0 $((NUM_SCHEDULERS - 1))); do
            kubectl -n "$NAMESPACE" logs deployment/para-scheduler-${i} \
                --since-time="$SINCE_TIME" --tail=-1 \
                > "$LOGS_DIR/scheduler-${i}.log" 2>&1 || true
        done
        kubectl -n "$NAMESPACE" logs deployment/para-binder \
            --since-time="$SINCE_TIME" --tail=-1 \
            > "$LOGS_DIR/binder.log" 2>&1 || true
        kubectl -n "$NAMESPACE" logs deployment/para-dispatcher \
            --since-time="$SINCE_TIME" --tail=-1 \
            > "$LOGS_DIR/dispatcher.log" 2>&1 || true
        echo "    Saved to $LOGS_DIR/ ($(ls "$LOGS_DIR"/*.log 2>/dev/null | wc -l) files)"
    fi

    # Save trial timing with phase breakdown
    cat > "$TRIAL_DIR/timing.json" <<TIMING
{
  "trial": $TRIAL,
  "overall": {"start": $START_TS, "end": $END_TS, "duration": $((END_TS - START_TS))},
  "saturation": {"start": ${SAT_START:-null}, "end": ${SAT_END:-null}},
  "latency": {"start": ${LAT_START:-null}, "end": ${LAT_END:-null}}
}
TIMING

    echo "  Trial $TRIAL completed in $((END_TS - START_TS))s."

    # Reset scheduler state between trials (not after last trial)
    if [ "$TRIAL" -lt "$NUM_TRIALS" ]; then
        echo ""
        reset_scheduler_state
    fi
done

# ============================================================
# Step 6: Final cleanup — delete workloads, nodes, reset state
# ============================================================
echo ""
echo "Step 6: Final cleanup..."
# Delete workloads BEFORE deleting KWOK nodes — if nodes are removed first,
# pods on those nodes can never gracefully terminate (no kubelet to handle
# the delete), causing namespaces to get stuck in Terminating.
kubectl delete deployments -l group=saturation --all-namespaces --ignore-not-found=true --wait=false 2>/dev/null || true
kubectl delete deployments -l group=latency --all-namespaces --ignore-not-found=true --wait=false 2>/dev/null || true

# Force-delete any pods still stuck on KWOK nodes (grace-period=0 skips kubelet).
# CL2 namespaces follow the pattern test-XXXXX-N.
echo "  Force-deleting remaining pods in CL2 namespaces (parallel)..."
CL2_NAMESPACES=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | grep '^test-' || true)
for ns in $CL2_NAMESPACES; do
    kubectl delete pods --all -n "$ns" --force --grace-period=0 --ignore-not-found=true 2>/dev/null &
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

# KWOK node cleanup.
# With --preserve-nodes, keep nodes alive for the next experiment (batch script
# guarantees the final experiment of a sweep does NOT set this flag, so the very
# last Step 6 still purges everything for a clean exit).
if [ "$PRESERVE_NODES" = true ]; then
    echo "  --preserve-nodes: keeping KWOK nodes alive for next experiment."
else
    # Use --wait to ensure nodes are fully removed from etcd before cleaning leases,
    # otherwise the subsequent lease deletion races with node controller.
    echo "  Deleting KWOK nodes (waiting for completion)..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true

    # Delete stale KWOK node leases.
    # When nodes are deleted and recreated across experiments, the lease objects in etcd
    # get new UIDs while KWOK still caches the old UIDs. This UID mismatch causes
    # "Precondition failed" errors on lease renewal, leading to NotReady nodes.
    # Note: KWOK shard restart is deferred to Step 1a of the next experiment (right
    # before node creation) to ensure a fresh Watch connection — restarting here would
    # leave the Watch idle for the duration of setup/config steps, risking the same
    # delayed-discovery problem that caused the s8 NotReady incident.
    echo "  Cleaning up KWOK node leases..."
    delete_kwok_leases
fi

# Clean up CL2-created test namespaces and wait for them to be fully removed.
# CL2 creates namespaces like test-XXXXX-N; if they get stuck in Terminating
# (e.g. due to leftover finalizers), force-finalize them.
echo "  Cleaning up CL2 test namespaces..."
CL2_TEST_NS=$(kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' | grep '^test-' || true)
for ns in $CL2_TEST_NS; do
    kubectl delete ns "$ns" --ignore-not-found=true --wait=false 2>/dev/null || true
done

CLEANUP_TIMEOUT=120
CLEANUP_START=$(date +%s)
while true; do
    TERMINATING_NS=$(kubectl get ns --field-selector=status.phase=Terminating -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
    # Filter to only test-* namespaces (CL2 created)
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
        echo "  Timeout waiting for namespaces to terminate, force-finalizing..."
        for ns in $CL2_TERMINATING; do
            kubectl get ns "$ns" -o json 2>/dev/null \
                | jq '.spec.finalizers = []' \
                | kubectl replace --raw "/api/v1/namespaces/$ns/finalize" -f - 2>/dev/null || true
        done
        # Brief wait for force-finalized namespaces to disappear
        sleep 5
        break
    fi

    echo "  Waiting for $(echo "$CL2_TERMINATING" | wc -w | xargs) CL2 namespaces to terminate... (${ELAPSED}s/${CLEANUP_TIMEOUT}s)"
    sleep 10
done

# Reset para-sched state for next experiment
"$DEPLOY_DIR/setup.sh" --clean || echo "  WARNING: setup.sh --clean failed (non-fatal)"

# Wait for API server to drain pending writes before starting next experiment.
# After large-scale cleanup (e.g. 290k pod deletions), etcd may still be processing
# async deletions (garbage collection, etc.) even after namespaces are gone.
# We poll until: (1) no KWOK nodes remain, (2) no test pods remain, and
# (3) API server responds promptly — indicating etcd backlog has cleared.
echo "  Waiting for API server to settle..."
settle_fully 300

TOTAL_END_TS=$(date +%s)
echo ""
echo "============================================"
echo "Experiment completed!"
echo "  Trials: $NUM_TRIALS"
echo "  Total time: $((TOTAL_END_TS - TOTAL_START_TS))s"
echo "  Results: $EXPERIMENT_DIR"
echo "============================================"
