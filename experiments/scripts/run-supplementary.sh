#!/bin/bash

# ============================================================
# Para-Sched application-layer benchmark (group D-new)
#
# The workload check the paper's discussion reports: Nginx, Redis and MySQL
# on the three physical workers, under Vanilla and ParKour in each paradigm
# (E2, E3, P1, P4) and three CPU-contention profiles (none, mild, heavy).
# 12 configs × 3 trials = 36 trials, ~5 h.
#
# Each trial is a full cycle of run-workload-bench.sh v3 (per-trial redeploy):
# redeploy → reschedule → record placement → benchmark, so each cell gets
# independent placement samples rather than repeated benchmarks of a single
# placement, which would measure benchmark noise only.
#
# KWOK nodes are purged first. Otherwise para-scheduler's informer cache still
# holds every emulated node and Filter+Scores all of them for each workload
# pod, which leaks scheduling overhead into the application metrics.
#
# The worker nodes, the master address and the Prometheus URL come from
# experiments/site.env or the environment; see run-workload-bench.sh.
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

# ========== Default parameters ==========
DRY_RUN=false
EXPERIMENT_GROUP="D-new"
WORKLOAD_TRIALS=3           # independent placements per (strategy, profile)
DEFRAG_BETWEEN=true
SKIP_WARMUP=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --group)              EXPERIMENT_GROUP="$2"; shift 2 ;;
        --workload-trials)    WORKLOAD_TRIALS="$2"; shift 2 ;;
        --dry-run)            DRY_RUN=true; shift ;;
        --no-defrag)          DEFRAG_BETWEEN=false; shift ;;
        --skip-warmup)        SKIP_WARMUP=true; shift ;;
        -h|--help)
            cat <<EOF
Usage: $0 [options]

Run the application-layer benchmark: 12 configs × \$WORKLOAD_TRIALS trials on the
3-worker physical cluster, estimated ~5 hours.

Options:
  --group D-new         The benchmark; the only group, and the default
  --workload-trials N   Independent placements per (strategy, profile) (default: 3)
                        Each placement includes 1 full redeploy + 180s benchmark
  --dry-run             Print the experiment plan only
  --no-defrag           Skip the etcd defrag after purging KWOK nodes (enabled by default)
  --skip-warmup         Skip the host warmup before the first benchmark

Examples:
  $0                          # the benchmark, as published
  $0 --workload-trials 5      # 5 placements per cell
  $0 --dry-run                # preview the plan

EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

case $EXPERIMENT_GROUP in
    D-new) ;;
    *) echo "Error: --group must be D-new (got '$EXPERIMENT_GROUP')"; exit 1 ;;
esac

echo "============================================"
echo "Para-Sched Application-Layer Benchmark"
echo "============================================"
echo "Group:                $EXPERIMENT_GROUP"
echo "Workload trials:      $WORKLOAD_TRIALS (per-trial-redeploy)"
echo "Dry-run:              $DRY_RUN"
echo "Skip warmup:          $SKIP_WARMUP"
echo "Defrag after purge:   $DEFRAG_BETWEEN"
echo "============================================"
echo ""

FAILED_EXPERIMENTS=()
TOTAL_RUN=0

# ============================================================
#  Helpers
# ============================================================
# defrag_etcd and delete_kwok_leases come from lib/common.sh.

# State: pass --warmup to run-workload-bench.sh ONLY for the first
# benchmark (host-level CPU governor / page cache cold-start). Subsequent
# benchmarks share the same physical worker state.
WORKLOAD_FIRST_WARMUP=true

# Run one workload benchmark via run-workload-bench.sh (v3 per-trial-redeploy).
# Args: label strategy_arg trials stress_profile
run_workload() {
    local label=$1 strategy_arg=$2 trials=$3 stress_profile=${4:-none}

    echo "----------------------------------------"
    echo "Real workload benchmark: $label (strategy=$strategy_arg, stress=$stress_profile, trials=$trials)"
    echo "----------------------------------------"

    local extra=(--strategy "$strategy_arg" --trials "$trials" --stress-profile "$stress_profile")
    if [ "$WORKLOAD_FIRST_WARMUP" = true ] && [ "$SKIP_WARMUP" != true ]; then
        extra+=(--warmup)
        WORKLOAD_FIRST_WARMUP=false
        echo "  (first D-new workload — passing --warmup for host cold-start mitigation)"
    fi

    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY RUN] ./run-workload-bench.sh ${extra[*]}"
        return
    fi

    TOTAL_RUN=$((TOTAL_RUN + 1))
    if "$SCRIPT_DIR/run-workload-bench.sh" "${extra[@]}"; then
        echo "[$label] OK"
    else
        echo "[$label] FAILED (continuing)"
        FAILED_EXPERIMENTS+=("$label")
    fi
    # run-workload-bench.sh handles its own namespace cleanup; no etcd
    # defrag needed (no large-scale KWOK churn here).
}

# Delete KWOK nodes without touching para-sched components (setup.sh --clean).
# The benchmarks deploy 37 pods to 3 physical workers; any KWOK node left in
# para-scheduler's informer cache would be Filter+Scored for every one of them.
clean_kwok_nodes() {
    echo "========================================"
    echo "Purging KWOK nodes (prevent KWOK state from leaking into the benchmark)"
    echo "========================================"
    local kwok_count
    kwok_count=$(kubectl get nodes -l type=kwok --no-headers 2>/dev/null | wc -l | xargs)
    if [ "$kwok_count" -eq 0 ]; then
        echo "  No KWOK nodes found, skipping."
        return
    fi
    echo "  Found $kwok_count KWOK nodes; deleting..."
    kubectl delete nodes -l type=kwok --ignore-not-found=true --wait=true --timeout=300s 2>/dev/null || true
    # Clean orphaned leases (K8s may leave node heartbeat leases behind).
    delete_kwok_leases
    # Defrag after the deletion storm (10000+ etcd tombstones).
    if [ "$DEFRAG_BETWEEN" = true ]; then
        defrag_etcd
    fi
    echo "  KWOK nodes purged."
}

# ============================================================
# Group D-new: per-trial-redeploy workload benchmark
# ============================================================
# Each (strategy, profile) cell produces $WORKLOAD_TRIALS independent
# placement samples. E3 is the standard ParKour event configuration
# (K=2, p=0.5).
#
# Order: outer profile (none → mild → heavy), inner strategy (E2 → E3 → P1 → P4).
# Running all 4 strategies within the same profile minimizes time-of-day drift.
run_D_new() {
    echo "=== Group D-new: per-trial-redeploy workload bench (12 configs × $WORKLOAD_TRIALS trials) ==="
    for profile in none mild heavy; do
        for strategy in E2 E3 P1 P4; do
            run_workload "D1-${strategy}-${profile}" "$strategy" "$WORKLOAD_TRIALS" "$profile"
        done
    done
}

# ============================================================
# Main
# ============================================================
if [ "$DRY_RUN" = false ]; then
    clean_kwok_nodes
fi
run_D_new

echo ""
echo "============================================"
echo "Application-layer benchmark completed!"
echo "  Group:     $EXPERIMENT_GROUP"
echo "  Total run: $TOTAL_RUN"
echo "  Failed:    ${#FAILED_EXPERIMENTS[@]}"
if [ ${#FAILED_EXPERIMENTS[@]} -gt 0 ]; then
    echo ""
    echo "Failed experiments:"
    for exp in "${FAILED_EXPERIMENTS[@]}"; do echo "  - $exp"; done
fi
echo "============================================"
