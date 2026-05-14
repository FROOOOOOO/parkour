#!/bin/bash

# ============================================================
# Para-Sched downstream batch experiment script (Boards B / C / D / E)
# Corresponds to experiments/design.md §6-§9.
#
# Prerequisites: run-sensitivity.sh (Board A Tier-0) must be run first and its
#                conclusions written into experiments/board-A-optima.yaml.
#                This script reads that file on startup and exits with an error
#                if any __TBD__ placeholders remain.
#
# Structure:
#   - B1   Low-contention scale-out (event-driven E1/E2/E3 × 3 scales)
#   - B2   High-contention scale-out (E1/E2/E3 + P1/P2/P3/P4 × 4 scales)
#   - B3   High-contention scheduler scale-out (E2/E3/P3/P4 × 10 scheduler counts)
#   - C-event    Event-driven ablation (Ab-E0..3)
#   - C-periodic Periodic-sync ablation (Ab-P0..3)
#   - D4   Sync-paradigm overhead comparison (Tier-3 optional, reuses part of B2)
#   - E    Real-workload scheduling quality validation (RW-E2 / RW-E3)
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OPTIMA_FILE="$PROJECT_ROOT/experiments/board-A-optima.yaml"

# ========== Default parameters ==========
DRY_RUN=false
EXPERIMENT_GROUP="all"
TRIALS=3
COLLECT_LOGS=false
SKIP_OPTIMA_CHECK=false
DEFRAG_BETWEEN=true       # run etcd defrag after every experiment
FINAL_CLEANUP=true        # delete KWOK nodes + setup --clean at script end
SKIP_WARMUP=false         # skip the one-shot cluster warmup run before the first experiment
VARIANCE="0.6"            # Capacity-variance default for every run_experiment call (chosen from V-pilot v2: E2 ACF 10.9%, P3 ACF 43%, CV <4%); overridden internally by V-pilot group

while [[ $# -gt 0 ]]; do
    case $1 in
        --group)              EXPERIMENT_GROUP="$2"; shift 2 ;;
        --trials)             TRIALS="$2"; shift 2 ;;
        --dry-run)            DRY_RUN=true; shift ;;
        --collect-logs)       COLLECT_LOGS=true; shift ;;
        --skip-optima-check)  SKIP_OPTIMA_CHECK=true; shift ;;
        --optima-file)        OPTIMA_FILE="$2"; shift 2 ;;
        --no-defrag)          DEFRAG_BETWEEN=false; shift ;;
        --no-final-cleanup)   FINAL_CLEANUP=false; shift ;;
        --skip-warmup)        SKIP_WARMUP=true; shift ;;
        --variance)           VARIANCE="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --group <name> [options]

Run Board B/C/D/E experiments. Prerequisite: run run-sensitivity.sh first and write conclusions into board-A-optima.yaml.

Options:
  --group GROUP          Experiment group:
                           tier1        = B1 + B2 + B3 + C-event
                           tier2        = C-periodic + E
                           all          = tier1 + tier2 + D4
                           B1 / B2 / B3 individual boards
                           C-event / C-periodic ablation groups
                           D4                      sync-paradigm overhead comparison (Tier-3)
                           E                        real-workload validation
                           V-pilot                  HC-V parameter sweep pilot (E2 + P1 × 4 V levels)
                         (default: all)
  --trials N             Number of repetitions per group (default: 3)
  --variance V           Capacity-variance level: 0 | 0.3 | 0.6 | 1.0 (default: 0.6)
                           V=0.6 is the default selected by V-pilot v2 (E2 ACF 10.9%, P3 ACF 43%).
                           Applies to all run_experiment calls; overridden inside the V-pilot group loop.
                           Pass --variance 0 to reproduce the old HC-1 homogeneous scenario.
  --dry-run              Print the experiment plan only
  --collect-logs         Collect scheduler/binder/dispatcher logs
  --optima-file PATH     Path to board-A-optima.yaml (default: experiments/board-A-optima.yaml)
  --skip-optima-check    Skip board-A-optima.yaml validation (use only with --dry-run or for debugging)
  --no-defrag            Disable per-experiment etcd defrag (enabled by default)
  --no-final-cleanup     Disable node and environment cleanup at script end (for debugging)
  --skip-warmup          Skip the one-shot cluster warmup before the first experiment (enabled by default)

Example:
  # Run Tier-1 core experiments (B1/B2/B3 + C-event)
  $0 --group tier1 --trials 3

  # HC-V pilot (4 V levels × E2+P1 × 3 trials = 24 trials, 5000n / 10 schedulers)
  $0 --group V-pilot --trials 3

  # After V*=0.6 is known, re-run B2 with V=0.6
  $0 --group B2 --variance 0.6 --trials 3

  # Dry-run only to inspect the plan
  $0 --group all --dry-run --skip-optima-check

EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ========== Read board-A-optima.yaml ==========
yaml_get() {
    # yaml_get <file> <section> <key> → stdout value
    local file=$1 section=$2 key=$3
    awk -v sect="${section}:" -v k="${key}:" '
        $0 == sect { in_sect=1; next }
        in_sect && /^[^[:space:]]/ { in_sect=0 }
        in_sect && $1 == k {
            val=$2
            sub(/#.*/, "", val)
            gsub(/[[:space:]]/, "", val)
            print val
            exit
        }
    ' "$file"
}

load_optima() {
    if [ ! -f "$OPTIMA_FILE" ]; then
        echo "Error: optima file not found: $OPTIMA_FILE"
        echo "Please run run-sensitivity.sh (Board A) first and write conclusions into that file."
        exit 1
    fi

    K_E=$(yaml_get       "$OPTIMA_FILE" "event_driven" "K")
    STRAT_E=$(yaml_get   "$OPTIMA_FILE" "event_driven" "strategy")
    P_E=$(yaml_get       "$OPTIMA_FILE" "event_driven" "penalty_weight")
    K_P=$(yaml_get       "$OPTIMA_FILE" "periodic"     "K")
    STRAT_P=$(yaml_get   "$OPTIMA_FILE" "periodic"     "strategy")
    P_P=$(yaml_get       "$OPTIMA_FILE" "periodic"     "penalty_weight")

    local missing=()
    for name in K_E STRAT_E P_E K_P STRAT_P P_P; do
        local val="${!name}"
        if [ -z "$val" ] || [ "$val" = "__TBD__" ]; then
            missing+=("$name")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "Error: the following fields in board-A-optima.yaml are missing or still __TBD__:"
        for m in "${missing[@]}"; do echo "  - $m"; done
        echo "Please run run-sensitivity.sh (Board A) to complete the parameter sweep before filling in conclusions."
        exit 1
    fi

    # strategy whitelist validation
    for s in "$STRAT_E" "$STRAT_P"; do
        case $s in
            QualityFirst|LatencyFirst|WeightedRandom|QualityFirstParSync|LatencyFirstParSync) ;;
            *) echo "Error: unknown strategy '$s' in optima file"; exit 1 ;;
        esac
    done
    # event-driven must not use strategies that require per-partition freshness signals
    # (they degrade under the event paradigm):
    #   - LatencyFirst         : sorts directly by freshness
    #   - QualityFirstParSync  : paper-style partition-grain selection
    #   - LatencyFirstParSync  : paper-style partition-grain selection
    case "$STRAT_E" in
        LatencyFirst|QualityFirstParSync|LatencyFirstParSync)
            echo "Error: event_driven.strategy=$STRAT_E is invalid (this strategy requires per-partition freshness signals and degrades under the event paradigm)"
            exit 1
            ;;
    esac
}

if [ "$SKIP_OPTIMA_CHECK" = false ]; then
    load_optima
else
    # Placeholder values for dry-run use
    K_E=2; STRAT_E="QualityFirst"; P_E=0.3
    K_P=2; STRAT_P="QualityFirst"; P_P=0.3
fi

echo "============================================"
echo "Para-Sched Batch Experiment Runner (Boards B/C/D/E)"
echo "============================================"
echo "Group:          $EXPERIMENT_GROUP"
echo "Trials:         $TRIALS"
echo "Variance (V):   $VARIANCE"
echo "Dry-run:        $DRY_RUN"
echo "Collect logs:   $COLLECT_LOGS"
echo "Optima file:    $OPTIMA_FILE"
echo ""
echo "Board-A optima (proposed-method params):"
echo "  event_driven:  K=$K_E  strategy=$STRAT_E  p=$P_E"
echo "  periodic:      K=$K_P  strategy=$STRAT_P  p=$P_P"
echo ""

FAILED_EXPERIMENTS=()
TOTAL_RUN=0

# ========== Wait for cluster cleanup ==========
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
        echo "  Waiting for $(echo "$cl2_ns" | wc -w | xargs) CL2 namespaces to terminate... (${elapsed}s/${timeout}s)"
        sleep 10
    done
}

# ========== Workload profiles ==========
# Low-contention: 29 pods/node, 1 CPU, 8Gi
LOW_PPN=29;  LOW_CPU="1000m";  LOW_MEM="8Gi"
# High-contention HC-1: 1 pod/node, 24CPU, 192Gi
HIGH_PPN=1;  HIGH_CPU="24000m"; HIGH_MEM="192Gi"

# ========== run helper ==========
# Args: name nodes schedulers K penalty strategy sync_period partitions sync_pattern trials ppn cpu mem
run_experiment() {
    local name=$1 nodes=$2 scheds=$3 k=$4 penalty=$5 strategy=$6
    local sync_period=$7 partitions=$8 sync_pattern=$9
    local trials=${10:-$TRIALS}
    local ppn=${11:-} cpu=${12:-} mem=${13:-}

    echo "----------------------------------------"
    echo "[$name] nodes=$nodes scheds=$scheds K=$k p=$penalty strategy=$strategy"
    echo "       sync=$sync_period partitions=$partitions pattern=$sync_pattern"
    echo "----------------------------------------"

    # Warmup whenever the (nodes, variance, schedulers) tuple changes vs last warmup:
    #   - node count change → KWOK purge+recreate cold start
    #   - variance change   → KWOK purge+recreate (different shard capacities)
    #   - scheduler count change → 10 schedulers have 10 informer caches to prime;
    #     a warmup at N=5 doesn't cover N=10's cold-start. Fixing the residual
    #     T1 anomaly observed in V-pilot v1 data.
    # Skipped under --dry-run / --skip-warmup.
    if [ "$DRY_RUN" = false ]; then
        maybe_warmup_before "$nodes" "$scheds"
    fi

    # --preserve-nodes: always pass. run-experiment.sh Step 1b auto-detects node count
    # AND capacity-variance mismatch (across B1/B2/B3 scales and HC-V sweeps) and
    # purges+recreates when needed; otherwise reuses. Final cleanup at script end
    # deletes everything.
    local extra=(--preserve-nodes)
    # CURRENT_VARIANCE can be set by individual groups (V-pilot) to override the
    # global --variance; falls back to the global value.
    local run_variance="${CURRENT_VARIANCE:-$VARIANCE}"
    extra+=("--variance" "$run_variance")
    [ -n "$ppn" ] && extra+=("--pods-per-node" "$ppn")
    [ -n "$cpu" ] && extra+=("--cpu-request" "$cpu")
    [ -n "$mem" ] && extra+=("--memory-request" "$mem")
    [ "$COLLECT_LOGS" = true ] && extra+=("--collect-logs")

    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY RUN] ./run-experiment.sh --name $name --nodes $nodes --schedulers $scheds \\"
        echo "            --backup $k --penalty $penalty --strategy $strategy \\"
        echo "            --sync-period $sync_period --partitions $partitions --sync-pattern $sync_pattern \\"
        echo "            --trials $trials ${extra[*]}"
        return
    fi

    TOTAL_RUN=$((TOTAL_RUN + 1))
    if "$SCRIPT_DIR/run-experiment.sh" \
        --name "$name" \
        --nodes "$nodes" --schedulers "$scheds" \
        --backup "$k" --penalty "$penalty" --strategy "$strategy" \
        --sync-period "$sync_period" --partitions "$partitions" --sync-pattern "$sync_pattern" \
        --trials "$trials" \
        "${extra[@]}"; then
        echo "[$name] OK"
    else
        echo "[$name] FAILED (continuing)"
        FAILED_EXPERIMENTS+=("$name")
    fi
    wait_for_clean_cluster
    # Defrag after every experiment (= every $trials trials) to prevent etcd bloat.
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
}

# Args: label strategy_arg trials [extra env for K_E/P_E/STRAT_E overrides]
run_workload() {
    local label=$1 strategy_arg=$2 trials=$3 stress_profile=${4:-none}

    echo "----------------------------------------"
    echo "Real workload benchmark: $label (strategy=$strategy_arg, stress=$stress_profile)"
    echo "----------------------------------------"

    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY RUN] CANDIDATE_K=${CANDIDATE_K:-} PENALTY=${PENALTY:-} STRATEGY_NAME=${STRATEGY_NAME:-} \\"
        echo "            ./run-workload-bench.sh --strategy $strategy_arg --trials $trials \\"
        echo "            --stress-profile $stress_profile"
        return
    fi

    TOTAL_RUN=$((TOTAL_RUN + 1))
    if "$SCRIPT_DIR/run-workload-bench.sh" --strategy "$strategy_arg" --trials "$trials" \
        --stress-profile "$stress_profile"; then
        echo "[$label] OK"
    else
        echo "[$label] FAILED (continuing)"
        FAILED_EXPERIMENTS+=("$label")
    fi
    wait_for_clean_cluster
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
}

# ========== etcd maintenance + final cleanup ==========
defrag_etcd() {
    # Use --full (compact + defrag) rather than --defrag-only.
    # K8s auto-compact runs every 5 min, so mid-sweep the last 5 min of writes
    # are NOT compacted and defrag cannot reclaim them. Explicit compact forces
    # everything to current revision before defrag. +~5s/call, keeps DB at minimum.
    echo "----------------------------------------"
    echo "etcd compact + defrag (post-experiment maintenance)"
    echo "----------------------------------------"
    if ! bash "$SCRIPT_DIR/etcd-maintenance.sh" --full 2>&1 | tail -15; then
        echo "  WARNING: etcd maintenance failed; continuing."
    fi
}

# ========== Cluster warmup (results discarded) ==========
# Two trigger scenarios:
#   1) The *initial* warmup at script start — handles the etcd/apiserver cold-start
#      after run-sensitivity.sh ends and leaves a large KWOK node pool empty
#      (defaults to 10000 nodes, aligned with the B2/B3 main battlefield).
#   2) Between two real experiments when the *node count* changes (e.g. B1 1000→2000→5000,
#      B2 2000→5000→10000→20000) — run-experiment.sh Step 1b purges old KWOK nodes and
#      creates new ones, which is itself a large-scale etcd write event. The real
#      first trial immediately following will cold-start again (observed: B2-2000n T1
#      throughput drops to 3-5 pods/s).
#
# Implementation: maintain the LAST_WARMUP_{NODES,VARIANCE,SCHEDS} triple; trigger
# warmup_at_nodes before run_experiment whenever any dimension changes, then update
# the variables. Initial values are empty, so warmup fires as needed before the first
# run_experiment call.
LAST_WARMUP_NODES=""
LAST_WARMUP_VARIANCE=""
LAST_WARMUP_SCHEDS=""

# warmup_at_nodes NODES — run one 60s throwaway experiment at the specified node scale;
# the result directory B-warmup-${NODES}n is deleted immediately, leaving no pollution
# in subsequent experiment data.
warmup_at_nodes() {
    local warm_nodes=$1
    local warm_scheds=${2:-5}
    local warm_name="B-warmup-${warm_nodes}n-N${warm_scheds}"
    # Honor CURRENT_VARIANCE so warmup and subsequent real experiment share the
    # same V and skip a pointless purge+recreate between them.
    local warm_variance="${CURRENT_VARIANCE:-$VARIANCE}"
    echo "----------------------------------------"
    echo "Warmup at ${warm_nodes} nodes, N=${warm_scheds}, V=${warm_variance} (full saturation, results discarded)"
    echo "----------------------------------------"
    if "$SCRIPT_DIR/run-experiment.sh" \
        --name "$warm_name" \
        --nodes "$warm_nodes" --schedulers "$warm_scheds" \
        --backup 0 --penalty 0.0 --strategy "QualityFirst" \
        --sync-period 0.1 --partitions 1 --sync-pattern diff \
        --trials 1 \
        --pods-per-node "$HIGH_PPN" --cpu-request "$HIGH_CPU" --memory-request "$HIGH_MEM" \
        --variance "$warm_variance" \
        --preserve-nodes 2>&1 | tail -30; then
        echo "  Warmup OK (${warm_nodes}n N=${warm_scheds} V=${warm_variance})"
    else
        echo "  Warmup FAILED at ${warm_nodes}n N=${warm_scheds} V=${warm_variance} (continuing — real experiments will still run)"
    fi
    rm -rf "$SCRIPT_DIR/../results/${warm_name}"
    wait_for_clean_cluster
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
    LAST_WARMUP_NODES="$warm_nodes"
    LAST_WARMUP_VARIANCE="$warm_variance"
    LAST_WARMUP_SCHEDS="$warm_scheds"
}

# warmup_cluster — initial warmup at script start (10000n, aligned with B2/B3 main battlefield).
warmup_cluster() {
    echo "========================================"
    echo "Initial cluster warmup"
    echo "========================================"
    warmup_at_nodes 10000
}

# maybe_warmup_before NODES — trigger warmup if the node count, variance, or scheduler count
# differs from the last warmup; otherwise skip.
# Under --skip-warmup, only updates LAST_WARMUP_NODES state without actually warming up.
maybe_warmup_before() {
    local target_nodes=$1
    local target_scheds=${2:-10}
    local target_variance="${CURRENT_VARIANCE:-$VARIANCE}"
    if [ "$SKIP_WARMUP" = true ]; then
        LAST_WARMUP_NODES="$target_nodes"
        LAST_WARMUP_VARIANCE="$target_variance"
        LAST_WARMUP_SCHEDS="$target_scheds"
        return
    fi
    if [ "$LAST_WARMUP_NODES" != "$target_nodes" ] \
       || [ "$LAST_WARMUP_VARIANCE" != "$target_variance" ] \
       || [ "$LAST_WARMUP_SCHEDS" != "$target_scheds" ]; then
        echo ">> Scale/V/N change: last=(${LAST_WARMUP_NODES:-<none>}n V=${LAST_WARMUP_VARIANCE:-<none>} N=${LAST_WARMUP_SCHEDS:-<none>}), next=(${target_nodes}n V=${target_variance} N=${target_scheds}) — inserting warmup."
        warmup_at_nodes "$target_nodes" "$target_scheds"
    fi
}

final_cleanup() {
    echo "========================================"
    echo "Final cleanup (script end)"
    echo "========================================"
    echo "Deleting all KWOK nodes..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
    KWOK_LEASES=$(kubectl -n kube-node-lease get leases -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^kwok-' || true)
    if [ -n "$KWOK_LEASES" ]; then
        echo "$KWOK_LEASES" | xargs kubectl -n kube-node-lease delete lease --ignore-not-found=true 2>/dev/null || true
    fi
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
    # next script invocation — the observed "DB size noticeably larger than before
    # the experiment" symptom.
    if [ "$DEFRAG_BETWEEN" = true ]; then
        defrag_etcd
    fi
    echo "Cluster is clean. Script ending."
}

# ============================================================
# Board B: end-to-end multi-dimensional comparison
# ============================================================

# B1: Low-contention scale-out (Tier-1)
# event-driven only: E1/E2/E3. E3 uses the Board A optimal K_E*/STRAT_E*/P_E*.
run_B1() {
    echo "=== Board B1: Low-contention scale-out (Tier-1) ==="
    for nodes in 1000 2000 5000; do
        run_experiment "B1-${nodes}n-E1" $nodes 1 0     0.0   "QualityFirst" 0.1 1 diff "$TRIALS" $LOW_PPN "$LOW_CPU" "$LOW_MEM"
        run_experiment "B1-${nodes}n-E2" $nodes 10 0     0.0   "QualityFirst" 0.1 1 diff "$TRIALS" $LOW_PPN "$LOW_CPU" "$LOW_MEM"
        run_experiment "B1-${nodes}n-E3" $nodes 10 $K_E  $P_E  "$STRAT_E"     0.1 1 diff "$TRIALS" $LOW_PPN "$LOW_CPU" "$LOW_MEM"
    done
}

# B2: High-contention (HC-1) scale-out (Tier-1)
run_B2() {
    echo "=== Board B2: High-contention scale-out (Tier-1) ==="
    for nodes in 2000 5000 10000 20000; do
        # Event-driven
        run_experiment "B2-${nodes}n-E1" $nodes 1 0    0.0  "QualityFirst" 0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B2-${nodes}n-E2" $nodes 10 0    0.0  "QualityFirst" 0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B2-${nodes}n-E3" $nodes 10 $K_E $P_E "$STRAT_E"     0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"

        # Periodic
        run_experiment "B2-${nodes}n-P1" $nodes 10 0    0.0  "QualityFirst" 1.0 1 glob "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B2-${nodes}n-P2" $nodes 10 0    0.0  "QualityFirst" 1.0 10 same "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B2-${nodes}n-P3" $nodes 10 0    0.0  "QualityFirst" 1.0 10 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B2-${nodes}n-P4" $nodes 10 $K_P $P_P "$STRAT_P"     1.0 10 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    done
}

# B3: High-contention scheduler scale-out (Tier-1)
# Fixed at 10000 nodes; varies scheduler count {2,4,6,8,10}. E1 omitted.
run_B3() {
    echo "=== Board B3: High-contention scheduler scale-out (Tier-1) ==="
    for n_sched in 2 4 6 8 10; do
        run_experiment "B3-N${n_sched}-E2" 10000 $n_sched 0    0.0  "QualityFirst" 0.1 1         diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B3-N${n_sched}-E3" 10000 $n_sched $K_E $P_E "$STRAT_E"     0.1 1         diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B3-N${n_sched}-P3" 10000 $n_sched 0    0.0  "QualityFirst" 1.0 $n_sched  diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        run_experiment "B3-N${n_sched}-P4" 10000 $n_sched $K_P $P_P "$STRAT_P"     1.0 $n_sched  diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    done
}

# ============================================================
# Board C: ablation experiments (all HC-1 / 10000 nodes / 10 schedulers)
# ============================================================

# Event-driven ablation (Tier-1)
# Ab-E0 = baseline E2        : K=0, p=0, strategy=QF
# Ab-E1 = +M (multicandidate): K=K_E*, p=0, strategy=QF (multi-candidate only)
# Ab-E2 = +P (penalty only)  : K=0, p=p_E*, strategy=QF
# Ab-E3 = +MP (= E3)         : K=K_E*, p=p_E*, strategy=STRAT_E*
run_C_event() {
    echo "=== Board C: Event-driven ablation (Tier-1) ==="
    run_experiment "AbE0-base" 10000 10 0     0.0  "QualityFirst" 0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    run_experiment "AbE1-M"    10000 10 $K_E  0.0  "QualityFirst" 0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    run_experiment "AbE2-P"    10000 10 0     $P_E "QualityFirst" 0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    run_experiment "AbE3-MP"   10000 10 $K_E  $P_E "$STRAT_E"     0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
}

# Periodic-sync ablation (Tier-2)
# Ab-P0 = baseline P3  : K=0, p=0, strategy=QF
# Ab-P1 = +M           : K=K_P*, p=0, strategy=QF
# Ab-P2 = +P           : K=0, p=p_P*, strategy=QF
# Ab-P3 = +MP (= P4)   : K=K_P*, p=p_P*, strategy=STRAT_P*
run_C_periodic() {
    echo "=== Board C: Periodic-sync ablation (Tier-2) ==="
    run_experiment "AbP0-base" 10000 10 0     0.0  "QualityFirst" 1.0 10 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    run_experiment "AbP1-M"    10000 10 $K_P  0.0  "QualityFirst" 1.0 10 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    run_experiment "AbP2-P"    10000 10 0     $P_P "QualityFirst" 1.0 10 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    run_experiment "AbP3-MP"   10000 10 $K_P  $P_P "$STRAT_P"     1.0 10 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
}

# ============================================================
# V-pilot: HC-V parameter sweep (heterogeneous-capacity baseline ACF response curve)
#
# Goal: determine V* before committing to all B/C scenarios — the capacity-variance
# level at which the ACF of the event and periodic baseline paradigms rises
# significantly.
#
# v2 spec (changes derived from v1 data; v1 E2 ACF peaked at ~7%, P1 at ~11%):
#   - 5000n / HC-1 pod spec (24 CPU / 192 Gi)
#   - N=10 schedulers (v1 used N=5; insufficient parallelism to convert V spread into conflicts)
#   - 4 V levels: {0, 0.3, 0.6, 1.0}
#   - 4 baselines:
#       E2 (event-driven, sync=0.1s)
#       P1 (periodic globSync,   G=1s, partitions=1)
#       P2 (periodic sameSync,   G=1s, partitions=10)
#       P3 (periodic diffSync,   G=1s, partitions=10)
#   - Recommended --trials 5 (v1's 3 trials were pierced by single bad-trial outliers)
# Total: 4 × 4 × 5 = 80 trials, ~3.5h
#
# V* selection criteria:
#   - E2 ACF >= 10% and P3 ACF >= 20%
#   - No SLO_Fail / no bind_success=0 anomalies
#   - Hot shard (s0) actual pod absorption ratio > 1.5× expected
# ============================================================
run_V_pilot() {
    local n_scheds=10
    local n_parts=10     # aligned with N=10; P3 diffSync assigns one partition per scheduler
    echo "=== V-pilot: HC-V parameter sweep (4 V × {E2,P1,P2,P3} × $TRIALS trials, N=${n_scheds}) ==="
    for v in 0 0.3 0.6 1.0; do
        CURRENT_VARIANCE="$v"
        local v_tag="V${v}"
        # E2: vanilla event-driven baseline
        run_experiment "Vpilot-${v_tag}-E2" 5000 $n_scheds 0 0.0 "QualityFirst" 0.1 1        diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        # P1: periodic globSync (partitions=1, all schedulers sync the full view)
        run_experiment "Vpilot-${v_tag}-P1" 5000 $n_scheds 0 0.0 "QualityFirst" 1.0 1        glob "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        # P2: periodic sameSync (partitions=N, all schedulers sync the same partition)
        run_experiment "Vpilot-${v_tag}-P2" 5000 $n_scheds 0 0.0 "QualityFirst" 1.0 $n_parts same "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
        # P3: periodic diffSync (partitions=N, each scheduler syncs its own partition)
        run_experiment "Vpilot-${v_tag}-P3" 5000 $n_scheds 0 0.0 "QualityFirst" 1.0 $n_parts diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    done
    unset CURRENT_VARIANCE
}

# ============================================================
# Board D4: sync-paradigm overhead comparison (Tier-3 optional)
# Reuses the E3 vs P4 comparison subset from B2; adds a more explicit
# paradigm-switch contrast here.
# Design doc §8.5 — may be skipped if time is tight.
# ============================================================
run_D4() {
    echo "=== Board D4: Sync-paradigm overhead comparison (Tier-3) ==="
    # Fixed at 10000 nodes / 10 schedulers / HC-1. Event-driven vs periodic-sync
    # (both use Board A optimal parameters).
    run_experiment "D4-event"    10000 10 $K_E $P_E "$STRAT_E" 0.1 1 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
    run_experiment "D4-periodic" 10000 10 $K_P $P_P "$STRAT_P" 1.0 10 diff "$TRIALS" $HIGH_PPN "$HIGH_CPU" "$HIGH_MEM"
}

# ============================================================
# Board E: real-workload scheduling quality validation (Tier-2)
# RW-E2: vanilla multi-scheduler (K=0, p=0)
# RW-E3: proposed event-driven, parameters from Board A
#
# 2026-04-22 redesign: for each strategy × 3 stress profiles (none/mild/heavy),
# 3×2=6 configurations validate that E3's Penalty benefit grows as conflict
# intensity increases.
# stress_profile is achieved by pre-pinning stress-ng pods to worker nodes via
# .spec.nodeName (bypassing para-scheduler), creating heterogeneous CPU pressure
# so the scheduler's placement decisions are truly "meaningful".
# ============================================================
run_E() {
    echo "=== Board E: Real-workload scheduling quality validation (Tier-2) ==="
    for profile in none mild heavy; do
        run_workload "RW-E2-${profile}" "E2" "$TRIALS" "$profile"
        CANDIDATE_K="$K_E" PENALTY="$P_E" STRATEGY_NAME="$STRAT_E" \
            run_workload "RW-E3-${profile}" "E3" "$TRIALS" "$profile"
    done
}

# ============================================================
# Main dispatch
# ============================================================
# Warmup is now driven by maybe_warmup_before() inside run_experiment: the first
# real experiment (and any subsequent one at a new node count) triggers a
# throwaway warmup at that scale. This eliminates both:
#   (a) the post-setup.sh cold-start before the very first experiment, and
#   (b) the per-scale cold-start after run-experiment.sh Step 1b's KWOK
#       purge+recreate (the residual B2-2000n/5000n T1 anomaly from v3→v4).
# No explicit script-start warmup needed; --skip-warmup still fully disables it.

case $EXPERIMENT_GROUP in
    tier1)
        run_B1; run_B2; run_B3; run_C_event ;;
    tier2)
        run_C_periodic; run_E ;;
    all)
        run_B1; run_B2; run_B3; run_C_event; run_C_periodic; run_E ;;
    B1)          run_B1 ;;
    B2)          run_B2 ;;
    B3)          run_B3 ;;
    C-event)     run_C_event ;;
    C-periodic)  run_C_periodic ;;
    D4)          run_D4 ;;
    E)           run_E ;;
    V-pilot)     run_V_pilot ;;
    *)
        echo "Error: unknown group '$EXPERIMENT_GROUP'"
        echo "Run $0 --help for available groups."
        exit 1
        ;;
esac

# Final cleanup: purge KWOK nodes + reset components so the cluster ends clean.
if [ "$FINAL_CLEANUP" = true ] && [ "$DRY_RUN" = false ]; then
    final_cleanup
fi

echo ""
echo "============================================"
echo "Batch experiments completed!"
echo "  Total run: $TOTAL_RUN"
echo "  Failed:    ${#FAILED_EXPERIMENTS[@]}"
if [ ${#FAILED_EXPERIMENTS[@]} -gt 0 ]; then
    echo ""
    echo "Failed experiments:"
    for exp in "${FAILED_EXPERIMENTS[@]}"; do echo "  - $exp"; done
fi
echo "============================================"
