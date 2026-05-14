#!/bin/bash

# ============================================================
# Para-Sched Supplementary Experiments Script
# Fills the experiment gaps marked in paper/eval-data.md §7:
#
#   - Group K-supp (§7.6)
#       The existing K-sweep periodic (S-P-K{0,1,2,4}) was run only
#       under sync_pattern=diff; this group adds glob (partitions=1)
#       and sameSync (partitions=10) along the same K dimension,
#       proving that ParKour's multi-candidate mechanism yields
#       monotonically positive gains across globSync / sameSync /
#       diffSync paradigms.
#       8 configs × 3 trials, KWOK 10000n / V=0.6 / 10 schedulers, ~30 min.
#
#   - Group D-new (§7.11)
#       The original D-new had 12 configs × 5 trials = 60 trials, but
#       each (strategy, profile) cell triggered only 1 scheduling
#       decision — 5 trials only measured application-layer benchmark
#       noise, not placement variance, making it impossible to separate
#       placement randomness from systematic scheduling effects.
#       This group uses the per-trial-redeploy logic from
#       run-workload-bench.sh v3: each trial redeploys → reschedules →
#       records placement → benchmarks, giving each cell N independent
#       placement samples.
#       12 configs × 3 trials = 36 trials, 3-worker physical cluster, ~5 h.
#
# Execution order design:
#   Recommended --group all: run K-supp first (30 min, fast) then
#   D-new (5 h, long-running). If the long run is interrupted, at
#   least K-supp data is already on disk.
#
# Relationship to batch-run.sh:
#   Helper functions (wait_for_clean_cluster / defrag_etcd /
#   warmup_at_nodes / maybe_warmup_before / final_cleanup) mirror
#   batch-run.sh to ensure consistent KWOK cluster state management.
#   board-A-optima.yaml is not read (K-supp uses a fixed K sweep and
#   does not depend on optimal values; D-new maps strategy to K/p
#   internally inside run-workload-bench.sh).
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ========== Default parameters ==========
DRY_RUN=false
EXPERIMENT_GROUP="all"
TRIALS=3                    # KWOK trials per group (K-supp)
WORKLOAD_TRIALS=3           # D-new independent placements per (strategy, profile)
COLLECT_LOGS=false
DEFRAG_BETWEEN=true
FINAL_CLEANUP=true
SKIP_WARMUP=false
VARIANCE="0.6"

while [[ $# -gt 0 ]]; do
    case $1 in
        --group)              EXPERIMENT_GROUP="$2"; shift 2 ;;
        --trials)             TRIALS="$2"; shift 2 ;;
        --workload-trials)    WORKLOAD_TRIALS="$2"; shift 2 ;;
        --dry-run)            DRY_RUN=true; shift ;;
        --collect-logs)       COLLECT_LOGS=true; shift ;;
        --no-defrag)          DEFRAG_BETWEEN=false; shift ;;
        --no-final-cleanup)   FINAL_CLEANUP=false; shift ;;
        --skip-warmup)        SKIP_WARMUP=true; shift ;;
        --variance)           VARIANCE="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --group <name> [options]

Run supplementary experiments for the gaps marked in eval-data §7.

Groups:
  K-supp                K-sweep periodic supplemented with glob + sameSync
                        (8 configs × \$TRIALS trials, KWOK 10000n / V=0.6)
                        Estimated ~30 minutes
  D-new                 D-new per-trial-redeploy
                        (12 configs × \$WORKLOAD_TRIALS trials, 3-worker physical cluster)
                        Estimated ~5 hours
  all                   K-supp followed by D-new (recommended)

Options:
  --trials N            KWOK trials per group (default: 3)
  --workload-trials N   Independent placements per (strategy, profile) for D-new (default: 3)
                        Each placement includes 1 full redeploy + 180s benchmark
  --variance V          Capacity-variance level (default: 0.6); applies to K-supp only
  --dry-run             Print the experiment plan only
  --collect-logs        Collect scheduler/binder/dispatcher logs for K-supp
  --no-defrag           Skip etcd defrag after each KWOK experiment (enabled by default)
  --no-final-cleanup    Skip KWOK node cleanup at script end (for debugging)
  --skip-warmup         Skip KWOK warmup before K-supp and host warmup before first D-new

Examples:
  $0 --group all                   # Recommended: K-supp then D-new (~5.5h)
  $0 --group K-supp                # K-supp only (~30min)
  $0 --group D-new --workload-trials 5   # D-new only, 5 placements per cell
  $0 --group all --dry-run         # Preview the plan

EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

case $EXPERIMENT_GROUP in
    K-supp|D-new|all) ;;
    *) echo "Error: --group must be K-supp | D-new | all (got '$EXPERIMENT_GROUP')"; exit 1 ;;
esac

echo "============================================"
echo "Para-Sched Supplementary Experiments Runner"
echo "============================================"
echo "Group:                $EXPERIMENT_GROUP"
echo "KWOK trials:          $TRIALS"
echo "Workload trials:      $WORKLOAD_TRIALS (per-trial-redeploy)"
echo "Variance (V):         $VARIANCE"
echo "Dry-run:              $DRY_RUN"
echo "Skip warmup:          $SKIP_WARMUP"
echo "Defrag between:       $DEFRAG_BETWEEN"
echo "Final cleanup:        $FINAL_CLEANUP"
echo "============================================"
echo ""

FAILED_EXPERIMENTS=()
TOTAL_RUN=0

# ============================================================
#  Helpers (mirror batch-run.sh)
# ============================================================

# Wait for any CL2 namespaces (test-*) to terminate before next experiment.
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

# etcd compact + defrag (post-experiment maintenance)
defrag_etcd() {
    echo "----------------------------------------"
    echo "etcd compact + defrag (post-experiment maintenance)"
    echo "----------------------------------------"
    if ! bash "$SCRIPT_DIR/etcd-maintenance.sh" --full 2>&1 | tail -15; then
        echo "  WARNING: etcd maintenance failed; continuing."
    fi
}

# Warmup state — triggers when (nodes, V, scheds) triple changes.
LAST_WARMUP_NODES=""
LAST_WARMUP_VARIANCE=""
LAST_WARMUP_SCHEDS=""

# Run a 60s throwaway experiment at the given (nodes, scheds) to prime
# KWOK / informer / etcd caches. Result directory is deleted afterward.
warmup_at_nodes() {
    local warm_nodes=$1
    local warm_scheds=${2:-10}
    local warm_name="B-warmup-${warm_nodes}n-N${warm_scheds}"
    local warm_variance="${CURRENT_VARIANCE:-$VARIANCE}"
    echo "----------------------------------------"
    echo "Warmup at ${warm_nodes} nodes, N=${warm_scheds}, V=${warm_variance} (results discarded)"
    echo "----------------------------------------"
    if "$SCRIPT_DIR/run-experiment.sh" \
        --name "$warm_name" \
        --nodes "$warm_nodes" --schedulers "$warm_scheds" \
        --backup 0 --penalty 0.0 --strategy "QualityFirst" \
        --sync-period 0.1 --partitions 1 --sync-pattern diff \
        --trials 1 \
        --pods-per-node 1 --cpu-request 24000m --memory-request 192Gi \
        --variance "$warm_variance" \
        --preserve-nodes 2>&1 | tail -30; then
        echo "  Warmup OK (${warm_nodes}n N=${warm_scheds} V=${warm_variance})"
    else
        echo "  Warmup FAILED at ${warm_nodes}n N=${warm_scheds} V=${warm_variance} (continuing)"
    fi
    rm -rf "$SCRIPT_DIR/../results/${warm_name}"
    wait_for_clean_cluster
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
    LAST_WARMUP_NODES="$warm_nodes"
    LAST_WARMUP_VARIANCE="$warm_variance"
    LAST_WARMUP_SCHEDS="$warm_scheds"
}

# Trigger warmup if (nodes, V, scheds) differs from last warmup.
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

# Run one KWOK experiment via run-experiment.sh.
# Args: name nodes schedulers K penalty strategy sync_period partitions sync_pattern trials ppn cpu mem
run_experiment() {
    local name=$1 nodes=$2 scheds=$3 k=$4 penalty=$5 strategy=$6
    local sync_period=$7 partitions=$8 sync_pattern=$9
    local trials=${10:-$TRIALS}
    local ppn=${11:-} cpu=${12:-} mem=${13:-}

    echo "----------------------------------------"
    echo "[$name] nodes=$nodes scheds=$scheds K=$k p=$penalty strategy=$strategy"
    echo "       sync=$sync_period partitions=$partitions pattern=$sync_pattern trials=$trials"
    echo "----------------------------------------"

    if [ "$DRY_RUN" = false ]; then
        maybe_warmup_before "$nodes" "$scheds"
    fi

    local extra=(--preserve-nodes)
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
    [ "$DEFRAG_BETWEEN" = true ] && defrag_etcd
}

# State: pass --warmup to run-workload-bench.sh ONLY for the first D-new
# call (host-level CPU governor / page cache cold-start). Subsequent
# D-new experiments share the same physical worker state.
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

# Final cleanup — only meaningful if we touched KWOK in this run (K-supp).
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
    if [ "$DEFRAG_BETWEEN" = true ]; then
        defrag_etcd
    fi
    echo "Cluster is clean. Script ending."
}

# ========== Workload profile (matches B2/B3/Ablation main experiments) ==========
# High-contention HC-1: 1 pod/node, 24 CPU, 192 Gi
HIGH_PPN=1; HIGH_CPU="24000m"; HIGH_MEM="192Gi"

# ============================================================
# Group K-supp: K-sweep periodic supplemented with globSync + sameSync
# ============================================================
# The existing K-sweep periodic (S-P-K{0,1,2,4}) only covers
# sync_pattern=diff / partitions=10. This group adds:
#   - globSync (partitions=1): all schedulers sync the full global view
#   - sameSync (partitions=10): all schedulers sync the same partition
#
# Configs are kept strictly consistent with the existing K-sweep diff
# rows (K=sweep, p=0.3, strategy=QF, 10000n / 10 schedulers / V=0.6,
# sync_period=1.0) so new data can be merged directly into the existing
# sensitivity table, yielding a complete K × sync_pattern robustness grid.
run_K_supp() {
    echo "=== Group K-supp: K-sweep periodic glob + same (8 configs × $TRIALS trials) ==="
    for K in 0 1 2 4; do
        # globSync: partitions=1, all schedulers see the same global view.
        run_experiment "S-P-K${K}-glob" 10000 10 "$K" 0.3 "QualityFirst" 1.0 1  glob "$TRIALS" "$HIGH_PPN" "$HIGH_CPU" "$HIGH_MEM"
        # sameSync: partitions=10, schedulers all read the same per-partition snapshot.
        run_experiment "S-P-K${K}-same" 10000 10 "$K" 0.3 "QualityFirst" 1.0 10 same "$TRIALS" "$HIGH_PPN" "$HIGH_CPU" "$HIGH_MEM"
    done
}

# ============================================================
# Group D-new: per-trial-redeploy workload benchmark
# ============================================================
# Uses run-workload-bench.sh v3 (per-trial-redeploy). Each
# (strategy, profile) cell now produces $WORKLOAD_TRIALS independent
# placement samples.
#
# Differences from the original v6 §9.1.4 D-new:
#   - Original: E2 / E3-pen0 / P1 / P4, 5 trials with same placement
#   - This version: E2 / E3 (standard K=2 p=0.5) / P1 / P4, 3 trials
#     with independent placements
#
# Order: outer profile (none → mild → heavy), inner strategy (E2 → E3 → P1 → P4).
# Running all 4 strategies within the same profile minimizes time-of-day drift.
# E3 = standard ParKour event (K=2, p=0.5); E3-pen0 ablation is not run separately.
run_D_new() {
    echo "=== Group D-new: per-trial-redeploy workload bench (12 configs × $WORKLOAD_TRIALS trials) ==="
    for profile in none mild heavy; do
        for strategy in E2 E3 P1 P4; do
            run_workload "D1-${strategy}-${profile}" "$strategy" "$WORKLOAD_TRIALS" "$profile"
        done
    done
}

# ============================================================
# Main dispatch
# ============================================================
# Clean KWOK nodes without touching para-sched components (setup.sh --clean).
# Used between K-supp and D-new: D-new benchmarks deploy 37 pods to 3 physical
# workers, but para-scheduler still has 10000+ KWOK nodes in its informer cache
# from K-supp. The scheduler would Filter+Score all 10000 nodes for every pod,
# leaking massive scheduling overhead into the application-layer benchmark.
# Deleting KWOK nodes (and their leases) before D-new eliminates this confound.
clean_kwok_nodes() {
    echo "========================================"
    echo "Purging KWOK nodes (pre-D-new: prevent KWOK state from leaking into benchmark)"
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
    local kwok_leases
    kwok_leases=$(kubectl -n kube-node-lease get leases -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
        | tr ' ' '\n' | grep '^kwok-' || true)
    if [ -n "$kwok_leases" ]; then
        echo "$kwok_leases" | xargs kubectl -n kube-node-lease delete lease --ignore-not-found=true 2>/dev/null || true
    fi
    # Defrag after the deletion storm (10000+ etcd tombstones).
    if [ "$DEFRAG_BETWEEN" = true ]; then
        defrag_etcd
    fi
    echo "  KWOK nodes purged."
}

case $EXPERIMENT_GROUP in
    K-supp)  run_K_supp ;;
    D-new)   run_D_new ;;
    all)
        run_K_supp
        # ── K-supp → D-new handover ──────────────────────────────────────
        # Must purge KWOK nodes before D-new, otherwise para-scheduler's
        # informer cache still contains 10000 emulated nodes and will
        # Filter+Score them for every workload pod on the 3-worker host.
        # This leaks massive scheduling overhead into application-layer
        # metrics and confounds the E2/E3/P1/P4 comparison.
        if [ "$DRY_RUN" = false ]; then
            clean_kwok_nodes
        fi
        run_D_new
        ;;
esac

# Final cleanup: only meaningful when K-supp ran and KWOK nodes still exist.
if [ "$FINAL_CLEANUP" = true ] && [ "$DRY_RUN" = false ]; then
    case $EXPERIMENT_GROUP in
        K-supp|all) final_cleanup ;;
        D-new) ;;  # no KWOK manipulation
    esac
fi

echo ""
echo "============================================"
echo "Supplementary experiments completed!"
echo "  Group:     $EXPERIMENT_GROUP"
echo "  Total run: $TOTAL_RUN"
echo "  Failed:    ${#FAILED_EXPERIMENTS[@]}"
if [ ${#FAILED_EXPERIMENTS[@]} -gt 0 ]; then
    echo ""
    echo "Failed experiments:"
    for exp in "${FAILED_EXPERIMENTS[@]}"; do echo "  - $exp"; done
fi
echo "============================================"
