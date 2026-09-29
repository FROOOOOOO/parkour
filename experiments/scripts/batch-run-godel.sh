#!/bin/bash

# ============================================================
# Godel baseline batch experiment script (boards B1/B2/B3)
#
# Runs the Godel baseline the figures compare against: the Godel board of
# experiments/registry.json, every cell with the parameters and trial count
# declared there, into the board's directory, stamped with its registry cell.
# Godel has no proposed-method parameters (no K/penalty/strategy/sync/
# partitions), so it runs only as the baseline of boards B1, B2 and B3.
#
# Groups (registry cells are the multi-scheduler E2 runs):
#   - B1   Low-contention scale sweep  (E2 × 3 scales)
#   - B2   High-contention scale sweep (E2 × 4 scales)
#   - B3   High-contention scheduler sweep (E2 × 5 N values @ 10k nodes)
#   - all  every board above
#
# Prerequisites:
#   - godel-scheduler/deploy/lab-cluster/setup.sh has been run (CRD/RBAC installed)
#   - Prometheus has godel scrape jobs configured
#   - run-godel-baseline.sh / collect-metrics-godel.sh / kwok-deployment-godel.yaml are in place
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
# Bypass any HTTP(S) proxy for the cluster network and Prometheus. Child
# processes (run-godel-baseline.sh, godel setup.sh, kubectl, curl) inherit it.
export_cluster_no_proxy
GODEL_DEPLOY_DIR="$PROJECT_ROOT/godel-scheduler/deploy/lab-cluster"

# ========== Default parameters ==========
DRY_RUN=false
EXPERIMENT_GROUP="all"
TRIALS=""                 # --trials N: overrides every cell's declared count
TRIALS_SET=false
COLLECT_LOGS=false
DEFRAG_BETWEEN=true
FINAL_CLEANUP=true
SKIP_WARMUP=false
# From the environment or experiments/site.env, else localhost.
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9091}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --group)             EXPERIMENT_GROUP="$2"; shift 2 ;;
        --trials)            TRIALS="$2"; TRIALS_SET=true; shift 2 ;;
        --dry-run)           DRY_RUN=true; shift ;;
        --collect-logs)      COLLECT_LOGS=true; shift ;;
        --no-defrag)         DEFRAG_BETWEEN=false; shift ;;
        --no-final-cleanup)  FINAL_CLEANUP=false; shift ;;
        --skip-warmup)       SKIP_WARMUP=true; shift ;;
        --prometheus-url)    PROMETHEUS_URL="$2"; shift 2 ;;
        --results-root)      RESULTS_ROOT="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --group <name> [options]

Run the Godel baseline cells of experiments/registry.json.

Options:
  --group GROUP          Experiment group:
                           all          = B1 + B2 + B3
                           B1 / B2 / B3 the registry's Godel cells of that board
                         (default: all)
  --trials N             Trials per cell (default: the registry's count)
  --dry-run              Print experiment plan only
  --collect-logs         Collect scheduler/binder/dispatcher logs
  --no-defrag            Disable etcd defrag after each experiment (enabled by default)
  --no-final-cleanup     Disable node and environment cleanup at script end (for debugging)
  --skip-warmup          Skip scale-change warmup
  --results-root DIR     Runs go to the Godel board's directory under DIR (default: experiments/results)
  --prometheus-url URL   Prometheus URL (default: $PROMETHEUS_URL)

Example:
  # Run every registry cell (B1+B2+B3)
  $0 --group all

  # Run the B2 cells only, with fewer trials than declared
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
if [ "$TRIALS_SET" = true ]; then
    echo "Trials:         $TRIALS"
else
    echo "Trials:         as declared (registry)"
fi
echo "Dry-run:        $DRY_RUN"
echo "Collect logs:   $COLLECT_LOGS"
echo "Prometheus:     $PROMETHEUS_URL"
echo "Results root:   $RESULTS_ROOT"
echo ""

FAILED_EXPERIMENTS=()
TOTAL_RUN=0
# Every Godel run goes to the Godel board's directory.
CURRENT_RESULTS_DIR=$(registry_dir godel)

# ========== Cluster lifecycle ==========
# wait_for_clean_cluster, defrag_etcd, the warmup (maybe_warmup_before) and
# final_cleanup come from lib/common.sh; warmups use the Godel runner.
WARMUP_RUNNER=godel

# ========== run helper ==========
# Args: name nodes scheds trials ppn cpu mem
# CURRENT_CELL (godel/cell) is the registry cell the run measures, and
# CURRENT_VARIANCE its declared capacity variance.
run_godel_experiment() {
    local name=$1 nodes=$2 scheds=$3 trials=$4 ppn=$5 cpu=$6 mem=$7
    local variance="$CURRENT_VARIANCE"

    echo "----------------------------------------"
    echo "[$name] nodes=$nodes scheds=$scheds trials=$trials  workload: ppn=$ppn cpu=$cpu mem=$mem  V=$variance"
    echo "----------------------------------------"

    # Trigger warmup if (nodes, scheds, V) changed since last warmup.
    [ "$DRY_RUN" = false ] && maybe_warmup_before "$nodes" "$scheds"

    local extra=(
        --results-dir "$CURRENT_RESULTS_DIR"
        --preserve-nodes
        --variance "$variance"
        --pods-per-node "$ppn"
        --cpu-request "$cpu"
        --memory-request "$mem"
        --prometheus-url "$PROMETHEUS_URL"
    )
    [ "$COLLECT_LOGS" = true ] && extra+=("--collect-logs")
    [ -n "${CURRENT_CELL:-}" ] && extra+=("--board" "${CURRENT_CELL%%/*}" "--cell" "${CURRENT_CELL#*/}")

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

# ============================================================
# The registry's Godel cells: the E2 baselines of boards B1, B2 and B3
# ============================================================
# run_godel_registry BOARD — the Godel cells whose name starts with BOARD,
# with the parameters experiments/registry.json declares; --trials overrides
# their count. A cell is named <run name>-godel.
run_godel_registry() {
    local board=$1 rows cell trials nodes scheds ppn cpu mem variance
    echo "=== Godel cells of board $board, from experiments/registry.json ==="
    rows=$(python3 "$EXPERIMENTS_DIR/common/registry.py" --rows godel --columns \
        cell,trials,num_nodes,num_schedulers,pods_per_node,cpu_request,memory_request,capacity_variance)
    while IFS=$'\t' read -r cell trials nodes scheds ppn cpu mem variance; do
        case "$cell" in "$board"-*) ;; *) continue ;; esac
        [ "$TRIALS_SET" = true ] && trials=$TRIALS
        CURRENT_VARIANCE=$variance
        CURRENT_CELL="godel/$cell"
        run_godel_experiment "${cell%-godel}" "$nodes" "$scheds" "$trials" "$ppn" "$cpu" "$mem"
    done <<< "$rows"
    unset CURRENT_VARIANCE CURRENT_CELL
}

# ============================================================
# Main dispatch
# ============================================================
case $EXPERIMENT_GROUP in
    all) run_godel_registry B1; run_godel_registry B2; run_godel_registry B3 ;;
    B1|B2|B3) run_godel_registry "$EXPERIMENT_GROUP" ;;
    *)
        echo "Error: unknown group '$EXPERIMENT_GROUP'"
        echo "Run $0 --help for available groups."
        exit 1
        ;;
esac

if [ "$FINAL_CLEANUP" = true ] && [ "$DRY_RUN" = false ]; then
    final_cleanup "$GODEL_DEPLOY_DIR/setup.sh"
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
