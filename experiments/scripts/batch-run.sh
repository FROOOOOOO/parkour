#!/bin/bash

# ============================================================
# Para-Sched batch experiment script: the registry boards
#
# Runs the boards behind the paper's figures from experiments/registry.json:
# every cell, with the parameters and trial count declared there, into the
# board's directory under the results root, stamped with its registry cell.
# The Godel baseline has its own driver, batch-run-godel.sh.
#
# Groups:
#   - B1   Low-contention scale-out
#   - B2   High-contention scale-out
#   - B3   High-contention scheduler scale-out
#   - K, P Board A: the K sweep and the penalty-weight sweep
#   - ablation   Board C ablation, both paradigms;
#                C-event / C-periodic run one paradigm
#   - registry (or all)   every board above
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

# ========== Default parameters ==========
DRY_RUN=false
EXPERIMENT_GROUP="registry"
TRIALS=""                 # --trials N: overrides every cell's declared count
TRIALS_SET=false
COLLECT_LOGS=false
DEFRAG_BETWEEN=true       # run etcd defrag after every experiment
FINAL_CLEANUP=true        # delete KWOK nodes + setup --clean at script end
SKIP_WARMUP=false         # skip the one-shot cluster warmup run before the first experiment

while [[ $# -gt 0 ]]; do
    case $1 in
        --group)              EXPERIMENT_GROUP="$2"; shift 2 ;;
        --trials)             TRIALS="$2"; TRIALS_SET=true; shift 2 ;;
        --dry-run)            DRY_RUN=true; shift ;;
        --collect-logs)       COLLECT_LOGS=true; shift ;;
        --no-defrag)          DEFRAG_BETWEEN=false; shift ;;
        --no-final-cleanup)   FINAL_CLEANUP=false; shift ;;
        --skip-warmup)        SKIP_WARMUP=true; shift ;;
        --results-root)       RESULTS_ROOT="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --group <name> [options]

Run the registry boards, the published matrix, as experiments/registry.json declares them.

Options:
  --group GROUP          Experiment group:
                           registry (or all)                  every registry board: B1 B2 B3 K P ablation
                           B1 / B2 / B3 / K / P / ablation    one registry board
                           C-event / C-periodic               the ablation, one paradigm
                         (default: registry)
  --trials N             Trials per cell (default: the registry's count)
  --dry-run              Print the experiment plan only
  --collect-logs         Collect scheduler/binder/dispatcher logs
  --results-root DIR     Each board's runs go to its directory under DIR (default: experiments/results)
  --no-defrag            Disable per-experiment etcd defrag (enabled by default)
  --no-final-cleanup     Disable node and environment cleanup at script end (for debugging)
  --skip-warmup          Skip the one-shot cluster warmup before the first experiment (enabled by default)

Example:
  # Run the published matrix
  $0 --group registry

  # One registry board, with fewer trials than declared
  $0 --group B2 --trials 3

  # Dry-run only to inspect the plan
  $0 --group registry --dry-run

EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "============================================"
echo "Para-Sched Batch Experiment Runner (registry boards)"
echo "============================================"
echo "Group:          $EXPERIMENT_GROUP"
if [ "$TRIALS_SET" = true ]; then
    echo "Trials:         $TRIALS"
else
    echo "Trials:         as declared (registry)"
fi
echo "Dry-run:        $DRY_RUN"
echo "Collect logs:   $COLLECT_LOGS"
echo "Results root:   $RESULTS_ROOT"
echo ""

FAILED_EXPERIMENTS=()
TOTAL_RUN=0

# ========== Cluster lifecycle ==========
# wait_for_clean_cluster, defrag_etcd, the warmup (maybe_warmup_before) and
# final_cleanup come from lib/common.sh.

# ========== run helper ==========
# Args: name nodes schedulers K penalty strategy sync_period partitions sync_pattern trials ppn cpu mem [seed]
# CURRENT_CELL (board/cell) is the registry cell the run measures, and
# CURRENT_VARIANCE its declared capacity variance.
run_experiment() {
    local name=$1 nodes=$2 scheds=$3 k=$4 penalty=$5 strategy=$6
    local sync_period=$7 partitions=$8 sync_pattern=$9
    local trials=${10}
    local ppn=${11:-} cpu=${12:-} mem=${13:-} seed=${14:-}

    echo "----------------------------------------"
    echo "[$name] nodes=$nodes scheds=$scheds K=$k p=$penalty strategy=$strategy"
    echo "       sync=$sync_period partitions=$partitions pattern=$sync_pattern"
    echo "----------------------------------------"

    # Warmup whenever the (nodes, variance, schedulers) tuple changes vs last warmup:
    #   - node count change → KWOK purge+recreate cold start
    #   - variance change   → KWOK purge+recreate (different shard capacities)
    #   - scheduler count change → 10 schedulers have 10 informer caches to prime;
    #     a warmup at N=5 doesn't cover N=10's cold-start. Fixing the residual
    #     first-trial anomaly observed in early pilot runs.
    # Skipped under --dry-run / --skip-warmup.
    if [ "$DRY_RUN" = false ]; then
        maybe_warmup_before "$nodes" "$scheds"
    fi

    # --results-dir: the board directory the current group writes to.
    # --preserve-nodes: always pass. run-experiment.sh Step 1b auto-detects node count
    # AND capacity-variance mismatch (across B1/B2/B3 scales) and purges+recreates
    # when needed; otherwise reuses. Final cleanup at script end deletes everything.
    local extra=(--results-dir "$CURRENT_RESULTS_DIR" --preserve-nodes)
    extra+=("--variance" "$CURRENT_VARIANCE")
    [ -n "$ppn" ] && extra+=("--pods-per-node" "$ppn")
    [ -n "$cpu" ] && extra+=("--cpu-request" "$cpu")
    [ -n "$mem" ] && extra+=("--memory-request" "$mem")
    [ "$COLLECT_LOGS" = true ] && extra+=("--collect-logs")
    [ -n "$seed" ] && extra+=("--strategy-seed" "$seed")
    [ -n "${CURRENT_CELL:-}" ] && extra+=("--board" "${CURRENT_CELL%%/*}" "--cell" "${CURRENT_CELL#*/}")

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

# ============================================================
# The registry boards: B1, B2, B3, K, P and the ablation (Board C)
# ============================================================
# Every cell of a board runs with the parameters experiments/registry.json
# declares, which are those of the runs behind the paper's figures.

# run_registry_board BOARD [PARADIGM] — the cells of BOARD, only those of
# PARADIGM (event | periodic) when given; --trials overrides their count.
run_registry_board() {
    local board=$1 paradigm=${2:-} rows
    local cell cell_paradigm trials nodes scheds k penalty strategy seed
    local period partitions pattern ppn cpu mem variance
    echo "=== Board $board, from experiments/registry.json${paradigm:+ ($paradigm paradigm)} ==="
    CURRENT_RESULTS_DIR=$(registry_dir "$board")
    rows=$(python3 "$EXPERIMENTS_DIR/common/registry.py" --rows "$board" --columns \
        cell,paradigm,trials,num_nodes,num_schedulers,num_backup,conflict_penalty,strategy,strategy_seed,sync_period,num_partitions,sync_pattern,pods_per_node,cpu_request,memory_request,capacity_variance)
    while IFS=$'\t' read -r cell cell_paradigm trials nodes scheds k penalty strategy seed \
            period partitions pattern ppn cpu mem variance; do
        [ -z "$cell" ] && continue
        if [ -n "$paradigm" ] && [ "$cell_paradigm" != "$paradigm" ]; then continue; fi
        [ "$TRIALS_SET" = true ] && trials=$TRIALS
        CURRENT_VARIANCE=$variance
        CURRENT_CELL="$board/$cell"
        run_experiment "$cell" "$nodes" "$scheds" "$k" "$penalty" "$strategy" \
            "$period" "$partitions" "$pattern" "$trials" "$ppn" "$cpu" "$mem" "$seed"
    done <<< "$rows"
    unset CURRENT_VARIANCE CURRENT_CELL
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
    registry|all)
        for board in B1 B2 B3 K P ablation; do run_registry_board "$board"; done ;;
    B1|B2|B3|K|P|ablation)
        run_registry_board "$EXPERIMENT_GROUP" ;;
    C-event)     run_registry_board ablation event ;;
    C-periodic)  run_registry_board ablation periodic ;;
    *)
        echo "Error: unknown group '$EXPERIMENT_GROUP'"
        echo "Run $0 --help for available groups."
        exit 1
        ;;
esac

# Final cleanup: purge KWOK nodes + reset components so the cluster ends clean.
if [ "$FINAL_CLEANUP" = true ] && [ "$DRY_RUN" = false ]; then
    final_cleanup "$PROJECT_ROOT/para-scheduler/deploy/lab-cluster/setup.sh"
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
