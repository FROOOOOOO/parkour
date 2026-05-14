#!/bin/bash

# Board A (Tier-0) parameter sensitivity pre-sweep
# Corresponds to experiments/design.md §5 — sweeps each dimension under the
# high-contention (HC-V) scenario to find the optimal K* / strategy* / p* for
# each synchronization paradigm (event-driven / periodic).
#
# Uses a greedy per-dimension strategy: K → strategy → p → confirm. Each step
# requires manually inspecting the previous step's results, selecting the best
# value, and passing it as a CLI argument to the next step. Final conclusions
# are written by hand into experiments/board-A-optima.yaml.
#
# Fixed environment: 10000 nodes / 5 schedulers / HC-V V=0.6 (1 pod/node,
# 24CPU, 192Gi, heterogeneous capacity).
# V=0.6 is the default selected by V-pilot v2 (baseline ACF raised to
# E2 ~11% / P3 ~43%, CV <4%).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ========== Default parameters ==========
PARADIGM="both"           # event | periodic | both
STEP="K"                  # K | strategy | p | confirm
K_STAR=""                 # required for steps: strategy / p / confirm
STRATEGY_STAR=""          # required for step: confirm (only when strategy* = WR or LF)
P_STAR=""                 # required for step: confirm
TRIALS=3
DRY_RUN=false
COLLECT_LOGS=false
DEFRAG_BETWEEN=true       # run etcd defrag after every experiment (= every 3 trials)
FINAL_CLEANUP=true        # delete KWOK nodes + setup --clean at script end
SKIP_WARMUP=false         # skip the one-shot cluster warmup run before the first experiment
VARIANCE="0.6"            # Capacity-variance level (HC-V); 0.6 = current default per V-pilot v2

# ========== Fixed environment (§5.3) ==========
NODES=10000
SCHEDS=10
HIGH_PPN=1
HIGH_CPU="24000m"
HIGH_MEM="192Gi"

# Paradigm-specific parameters
EVENT_SYNC_PERIOD="0.1"
EVENT_PARTITIONS=1
EVENT_SYNC_PATTERN="diff"    # value is irrelevant when sync is disabled; run-experiment.sh distinguishes via backup/penalty
PERIODIC_SYNC_PERIOD="1.0"
PERIODIC_PARTITIONS=10
PERIODIC_SYNC_PATTERN="diff"

# ========== Argument parsing ==========
while [[ $# -gt 0 ]]; do
    case $1 in
        --paradigm)       PARADIGM="$2"; shift 2 ;;
        --step)           STEP="$2"; shift 2 ;;
        --k-star)         K_STAR="$2"; shift 2 ;;
        --strategy-star)  STRATEGY_STAR="$2"; shift 2 ;;
        --p-star)         P_STAR="$2"; shift 2 ;;
        --trials)         TRIALS="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=true; shift ;;
        --collect-logs)   COLLECT_LOGS=true; shift ;;
        --no-defrag)      DEFRAG_BETWEEN=false; shift ;;
        --no-final-cleanup) FINAL_CLEANUP=false; shift ;;
        --skip-warmup)    SKIP_WARMUP=true; shift ;;
        --variance)       VARIANCE="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --step <K|strategy|p|confirm> [options]

Board A parameter sensitivity sweep (§5). Dimensions swept in order: K → strategy → p → confirm.

Options:
  --paradigm PARADIGM        event | periodic | both (default: both)
  --step STEP                K | strategy | p | confirm (default: K)
  --k-star K                 optimal K from previous step (required for strategy/p/confirm)
  --strategy-star STRATEGY   optimal strategy from previous step (required for confirm)
                             event:    QualityFirst | WeightedRandom
                             periodic: QualityFirst | LatencyFirst | WeightedRandom |
                                       QualityFirstParSync | LatencyFirstParSync
  --p-star P                 optimal p from previous step (required for confirm)
  --trials N                 repetitions per configuration (default: 3)
  --dry-run                  print the experiment plan only, do not execute
  --collect-logs             collect scheduler/binder/dispatcher logs
  --no-defrag                disable etcd defrag after each experiment (enabled by default)
  --no-final-cleanup         disable node + environment cleanup at script end (useful for debug iteration)
  --skip-warmup              skip the one-shot cluster warmup before the first experiment (enabled by default)
  --variance V               Capacity-variance: 0 | 0.3 | 0.6 | 1.0 (default: 0.6)
                             V=0 reproduces the old HC-1 homogeneous scenario

Sweep steps:
  Step 1 (K):        fix strategy=QualityFirst, p=0.3; sweep K ∈ {0, 1, 2, 4}
                     → select K* (e.g. largest K before conflict rate notably drops)
  Step 2 (strategy): fix K=K*, p=0.3; sweep all strategies for the paradigm
                     event:    QF/WR × diff
                     periodic: QF/LF/WR × {glob,same,diff} + (QF/LF)ParSync × {same,diff}
                     → select strategy*
  Step 3 (p):        fix K=K*, strategy=QualityFirst; sweep p ∈ {0.0, 0.1, 0.3, 0.5, 0.7}
                     → select p*
  Step 4 (confirm):  run one confirmation group with K*/strategy*/p* only when strategy* ≠ QualityFirst
                     ParSync-family strategies skip sync_pattern=glob in confirm (degenerate case)

Recommended execution sequence:
  # Step 1: K sweep (both paradigms)
  $0 --step K

  # After analyzing results/S-E-K* and S-P-K*, e.g. K_E*=2, K_P*=2
  # Step 2: strategy sweep
  $0 --step strategy --paradigm event    --k-star 2
  $0 --step strategy --paradigm periodic --k-star 2

  # Step 3: p sweep (p curve under QualityFirst only)
  $0 --step p --paradigm event    --k-star 2
  $0 --step p --paradigm periodic --k-star 2

  # Step 4: confirm (only when strategy* ≠ QF)
  $0 --step confirm --paradigm event --k-star 2 --strategy-star WeightedRandom --p-star 0.3

When done, write conclusions into experiments/board-A-optima.yaml.
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Basic validation
case $PARADIGM in
    event|periodic|both) ;;
    *) echo "Error: --paradigm must be event|periodic|both"; exit 1 ;;
esac
case $STEP in
    K|strategy|p|confirm) ;;
    *) echo "Error: --step must be K|strategy|p|confirm"; exit 1 ;;
esac
if [ "$STEP" != "K" ] && [ -z "$K_STAR" ]; then
    echo "Error: step=$STEP requires --k-star"; exit 1
fi
if [ "$STEP" = "confirm" ] && { [ -z "$STRATEGY_STAR" ] || [ -z "$P_STAR" ]; }; then
    echo "Error: step=confirm requires --strategy-star and --p-star"; exit 1
fi

echo "============================================"
echo "Board A Sensitivity Sweep"
echo "============================================"
echo "Paradigm: $PARADIGM"
echo "Step:     $STEP"
[ -n "$K_STAR" ]        && echo "K*:        $K_STAR"
[ -n "$STRATEGY_STAR" ] && echo "strategy*: $STRATEGY_STAR"
[ -n "$P_STAR" ]        && echo "p*:        $P_STAR"
echo "Trials:   $TRIALS"
echo "Variance: $VARIANCE"
echo "Dry-run:  $DRY_RUN"
echo ""

FAILED=()
TOTAL_RUN=0

# ========== etcd maintenance + final cleanup ==========
defrag_etcd() {
    # Use --full (compact + defrag) rather than --defrag-only.
    # Rationale: K8s auto-compact runs every 5 min. Between experiments (runs taking
    # 5-10 min), the last ~5 min of writes are NOT compacted, so defrag cannot reclaim
    # them. Explicit compact-to-current-revision forces everything to free space first,
    # then defrag reclaims all of it. Adds ~5s per call but keeps DB near minimum.
    echo "----------------------------------------"
    echo "etcd compact + defrag (post-experiment maintenance)"
    echo "----------------------------------------"
    # NOTE: `if ! cmd | tail` tests the exit code of `tail` (always 0), masking
    # etcd-maintenance.sh failures. Use PIPESTATUS to capture the real rc.
    bash "$SCRIPT_DIR/etcd-maintenance.sh" --full 2>&1 | tail -15
    local rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "  WARNING: etcd maintenance failed (rc=$rc); continuing."
    fi
}

# ========== One-shot cluster warmup (results discarded) ==========
# Fixes the low throughput seen in trial-1 of the first experiment: after setup.sh
# the apiserver watch cache, etcd page cache, kwok heartbeat loop, and component
# informers are all cold. The initial 10k-node batch scheduling absorbs this
# cold-start cost, causing only the very first trial of run-sensitivity to show
# noticeably lower throughput (subsequent experiments are unaffected).
# Running a small throwaway experiment brings these caches to steady state; the
# results directory is deleted afterwards so process-results.py / plot_figures.py
# never see it.
#
# Configuration rationale:
#   --trials 1                              a single throwaway run is enough to cover the cold-start window
#   --backup 0 --penalty 0.0 QF             neutral parameters; does not trigger any multicandidate/penalty paths
#   --sync-period 1.0 --partitions 10       periodic paradigm covers ParSync-specific cold paths:
#                                           Dispatcher assigns partition-ids to 10k nodes (10-20ms/node
#                                           × 10k ≈ 1-2 min cold-start cost), Binder snapshot
#                                           ConfigMap chunked write, scheduler ParSync informer
#                                           bootstrap. The event-driven path is a subset of periodic
#                                           (informer/watch cache is shared), so the periodic warmup
#                                           automatically covers it — no separate event warmup needed.
#   --preserve-nodes                        keep the 10k KWOK nodes for subsequent experiments
# Artifact cleanup: rm -rf results/S-warmup removes all traces.
# Note: defrag is NOT run after warmup — defrag rebuilds the BoltDB file and
# invalidates OS mmap page cache, undoing part of what warmup just heated up.
# The warmup write volume is small (≈1 experiment), so skipping defrag here has
# no impact; the regular defrag after the first real experiment cleans up the
# warmup garbage as well.
warmup_cluster() {
    echo "========================================"
    echo "Cluster warmup (one-shot, results discarded)"
    echo "========================================"
    if "$SCRIPT_DIR/run-experiment.sh" \
        --name "S-warmup" \
        --nodes "$NODES" --schedulers "$SCHEDS" \
        --backup 0 --penalty 0.0 --strategy "QualityFirst" \
        --sync-period "$PERIODIC_SYNC_PERIOD" --partitions "$PERIODIC_PARTITIONS" --sync-pattern diff \
        --trials 1 \
        --pods-per-node "$HIGH_PPN" --cpu-request "$HIGH_CPU" --memory-request "$HIGH_MEM" \
        --variance "$VARIANCE" \
        --preserve-nodes 2>&1 | tail -30; then
        echo "  Warmup OK"
    else
        echo "  Warmup FAILED (continuing — real experiments will still run)"
    fi
    # Purge warmup results so process-results.py / plot_figures.py never see them.
    rm -rf "$SCRIPT_DIR/../results/S-warmup"
    wait_for_clean_cluster
}

final_cleanup() {
    echo "========================================"
    echo "Final cleanup (script end)"
    echo "========================================"
    echo "Deleting all KWOK nodes..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
    # Clean KWOK leases left over from deleted nodes
    KWOK_LEASES=$(kubectl -n kube-node-lease get leases -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^kwok-' || true)
    if [ -n "$KWOK_LEASES" ]; then
        echo "$KWOK_LEASES" | xargs kubectl -n kube-node-lease delete lease --ignore-not-found=true 2>/dev/null || true
    fi
    # Restart components to fully reset, drop CRD instances, clear snapshot CMs
    echo "Invoking setup.sh --clean to reset para-sched components..."
    local setup_sh="$SCRIPT_DIR/../../para-scheduler/deploy/lab-cluster/setup.sh"
    if [ -x "$setup_sh" ]; then
        bash "$setup_sh" --clean 2>&1 | tail -10 || true
    else
        echo "  (setup.sh not found at $setup_sh, skipping)"
    fi
    # Final defrag AFTER deletion storm: deleting 10k+ KWOK nodes + leases + CRD
    # instances generates etcd tombstones that the per-experiment defrag loop never
    # sees (it already exited). Without this, etcd DB size stays inflated for the
    # next script invocation — matches the observed "DB size noticeably larger
    # than before the experiment" symptom.
    if [ "$DEFRAG_BETWEEN" = true ]; then
        defrag_etcd
    fi
    echo "Cluster is clean. Script ending."
}

# ========== Wait for cluster to settle ==========
wait_for_clean_cluster() {
    local timeout=180 start=$(date +%s)
    while true; do
        local terminating=$(kubectl get ns --field-selector=status.phase=Terminating -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
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
        echo "  Waiting for CL2 namespaces to terminate... (${elapsed}s/${timeout}s)"
        sleep 10
    done
}

# ========== run helper: name K p strategy paradigm [sync_pattern] ==========
run_one() {
    local name=$1 k=$2 penalty=$3 strategy=$4 paradigm=$5 sync_pattern_override=$6

    local sync_period partitions sync_pattern
    case $paradigm in
        event)
            sync_period=$EVENT_SYNC_PERIOD
            partitions=$EVENT_PARTITIONS
            sync_pattern=$EVENT_SYNC_PATTERN
            ;;
        periodic)
            sync_period=$PERIODIC_SYNC_PERIOD
            partitions=$PERIODIC_PARTITIONS
            # If sync_pattern override provided, use it; otherwise use default
            if [ -n "$sync_pattern_override" ]; then
                sync_pattern=$sync_pattern_override
            else
                sync_pattern=$PERIODIC_SYNC_PATTERN
            fi
            ;;
    esac

    echo "----------------------------------------"
    echo "[$name] K=$k p=$penalty strategy=$strategy paradigm=$paradigm"
    echo "----------------------------------------"

    # --preserve-nodes: keep KWOK nodes across consecutive experiments with same node count.
    # All sensitivity experiments use the same fixed NODES (10000), so preserve always.
    # Final cleanup at script end deletes them.
    local extra_args=(--preserve-nodes)
    [ "$COLLECT_LOGS" = true ] && extra_args+=("--collect-logs")

    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY RUN] ./run-experiment.sh --name $name --nodes $NODES --schedulers $SCHEDS \\"
        echo "            --backup $k --penalty $penalty --strategy $strategy \\"
        echo "            --sync-period $sync_period --partitions $partitions --sync-pattern $sync_pattern \\"
        echo "            --trials $TRIALS --variance $VARIANCE \\"
        echo "            --pods-per-node $HIGH_PPN --cpu-request $HIGH_CPU --memory-request $HIGH_MEM \\"
        echo "            ${extra_args[*]}"
        return
    fi

    TOTAL_RUN=$((TOTAL_RUN + 1))
    if "$SCRIPT_DIR/run-experiment.sh" \
        --name "$name" \
        --nodes "$NODES" --schedulers "$SCHEDS" \
        --backup "$k" --penalty "$penalty" --strategy "$strategy" \
        --sync-period "$sync_period" --partitions "$partitions" --sync-pattern "$sync_pattern" \
        --trials "$TRIALS" --variance "$VARIANCE" \
        --pods-per-node "$HIGH_PPN" --cpu-request "$HIGH_CPU" --memory-request "$HIGH_MEM" \
        "${extra_args[@]}"; then
        echo "[$name] OK"
    else
        echo "[$name] FAILED (continuing)"
        FAILED+=("$name")
    fi
    wait_for_clean_cluster
    # Defrag after every experiment (= every $TRIALS trials) to prevent etcd bloat.
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
}

# ========== Sweep steps ==========
# Step 1: K sweep (strategy=QualityFirst, p=0.3 fixed)
sweep_K() {
    local paradigm=$1 tag   # paradigm ∈ {event, periodic}
    case $paradigm in event) tag="E" ;; periodic) tag="P" ;; esac
    echo "=== Step K sweep (paradigm=$paradigm) ==="
    for k in 0 1 2 4; do
        run_one "S-${tag}-K${k}" "$k" 0.3 "QualityFirst" "$paradigm"
    done
}

# Step 2: strategy sweep (K=K*, p=0.3 fixed)
# Periodic mode: sweeps all three sync patterns (glob / same / diff); event mode: diff only.
sweep_strategy() {
    local paradigm=$1 tag
    case $paradigm in event) tag="E" ;; periodic) tag="P" ;; esac
    echo "=== Step strategy sweep (paradigm=$paradigm, K=$K_STAR) ==="

    local strategies
    case $paradigm in
        event)    strategies="QualityFirst WeightedRandom" ;;
        periodic) strategies="QualityFirst LatencyFirst WeightedRandom" ;;
    esac

    local sync_patterns
    case $paradigm in
        event)    sync_patterns="diff" ;;
        periodic) sync_patterns="glob same diff" ;;
    esac

    for s in $strategies; do
        for sp in $sync_patterns; do
            run_one "S-${tag}-Strategy-${s}-${sp}" "$K_STAR" 0.3 "$s" "$paradigm" "$sp"
        done
    done

    # ParSync (ATC'21) partition-grain strategies: only meaningful under the periodic paradigm.
    # Skip sync_pattern=glob — under globSync all partition freshness values are identical,
    # so LatencyFirstParSync degenerates to single-partition random sampling and
    # QualityFirstParSync can only sort by partition average score (nearly identical to
    # QualityFirst-glob), providing no additional comparison value.
    if [ "$paradigm" = "periodic" ]; then
        for s in QualityFirstParSync LatencyFirstParSync; do
            for sp in same diff; do
                run_one "S-${tag}-Strategy-${s}-${sp}" "$K_STAR" 0.3 "$s" "$paradigm" "$sp"
            done
        done
    fi
}

# Step 3: p sweep (K=K*, strategy=QualityFirst fixed)
# Periodic mode: sweeps all three sync patterns (glob / same / diff); event mode: diff only.
sweep_p() {
    local paradigm=$1 tag
    case $paradigm in event) tag="E" ;; periodic) tag="P" ;; esac
    echo "=== Step p sweep (paradigm=$paradigm, K=$K_STAR, strategy=QualityFirst) ==="

    local sync_patterns
    case $paradigm in
        event)    sync_patterns="diff" ;;
        periodic) sync_patterns="glob same diff" ;;
    esac

    for p in 0.0 0.1 0.3 0.5 0.7; do
        local p_str=$(echo "$p" | sed 's/\.//g')   # 0.3 -> 03
        for sp in $sync_patterns; do
            run_one "S-${tag}-P${p_str}-${sp}" "$K_STAR" "$p" "QualityFirst" "$paradigm" "$sp"
        done
    done
}

# Step 4: confirm (run one K*/strategy*/p* group only when strategy* ≠ QF)
# Periodic mode: runs all three sync patterns (glob / same / diff); event mode: diff only.
# Exception: ParSync-family strategies skip glob in confirm (degenerate — see sweep_strategy comment).
sweep_confirm() {
    local paradigm=$1 tag
    case $paradigm in event) tag="E" ;; periodic) tag="P" ;; esac

    if [ "$STRATEGY_STAR" = "QualityFirst" ]; then
        echo "[$paradigm] strategy*=QualityFirst — skipping confirm (already covered by p sweep)"
        return
    fi
    echo "=== Step confirm (paradigm=$paradigm, K=$K_STAR, strategy=$STRATEGY_STAR, p=$P_STAR) ==="

    local sync_patterns
    case $paradigm in
        event)    sync_patterns="diff" ;;
        periodic)
            case $STRATEGY_STAR in
                QualityFirstParSync|LatencyFirstParSync) sync_patterns="same diff" ;;
                *)                                       sync_patterns="glob same diff" ;;
            esac
            ;;
    esac

    for sp in $sync_patterns; do
        run_one "S-${tag}-Confirm-${STRATEGY_STAR}-${sp}" "$K_STAR" "$P_STAR" "$STRATEGY_STAR" "$paradigm" "$sp"
    done
}

# ========== Main entry point ==========
dispatch_step() {
    local paradigm=$1
    case $STEP in
        K)        sweep_K        "$paradigm" ;;
        strategy) sweep_strategy "$paradigm" ;;
        p)        sweep_p        "$paradigm" ;;
        confirm)  sweep_confirm  "$paradigm" ;;
    esac
}

# One-shot cluster warmup before the first real experiment (fixes the first-trial
# cold-start throughput dip). Skipped in dry-run and via --skip-warmup.
if [ "$DRY_RUN" = false ] && [ "$SKIP_WARMUP" = false ]; then
    warmup_cluster
fi

case $PARADIGM in
    event)    dispatch_step event ;;
    periodic) dispatch_step periodic ;;
    both)     dispatch_step event; dispatch_step periodic ;;
esac

# Final cleanup: purge KWOK nodes + reset components so the cluster ends clean.
if [ "$FINAL_CLEANUP" = true ] && [ "$DRY_RUN" = false ]; then
    final_cleanup
fi

echo ""
echo "============================================"
echo "Sensitivity sweep completed!"
echo "  Total runs: $TOTAL_RUN"
echo "  Failed:     ${#FAILED[@]}"
if [ ${#FAILED[@]} -gt 0 ]; then
    echo ""
    echo "Failed experiments:"
    for exp in "${FAILED[@]}"; do echo "  - $exp"; done
fi
echo "============================================"
echo ""
echo "Next steps:"
case $STEP in
    K)
        echo "  1. Review metrics (conflict rate / throughput / CPU) under experiments/results/S-*-K*/"
        echo "  2. Select K_E* and K_P* according to §5.5 criteria"
        echo "  3. Run: $0 --step strategy --paradigm event    --k-star <K_E*>"
        echo "          $0 --step strategy --paradigm periodic --k-star <K_P*>"
        ;;
    strategy)
        echo "  1. Review metrics under S-*-Strategy-*/ and select strategy_E* and strategy_P*"
        echo "  2. Run: $0 --step p --paradigm event    --k-star $K_STAR"
        echo "          $0 --step p --paradigm periodic --k-star $K_STAR"
        ;;
    p)
        echo "  1. Review metrics under S-*-P*/ and select p_E* and p_P*"
        echo "  2. If strategy_{E|P}* ≠ QualityFirst, run:"
        echo "     $0 --step confirm --paradigm <event|periodic> --k-star $K_STAR --strategy-star <strategy*> --p-star <p*>"
        echo "  3. Write K* / strategy* / p* into experiments/board-A-optima.yaml"
        ;;
    confirm)
        echo "  Write K* / strategy* / p* into experiments/board-A-optima.yaml, then run run-experiments.sh"
        ;;
esac
