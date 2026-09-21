#!/usr/bin/env bash

# Dedicated entry point for Board H (synchronization-channel freshness
# measurement). See "Board H" in experiment-design.md.
#
# It measures the age, at the moment of installation, of the two state
# synchronization channels, using one shared set of histogram buckets:
#   scheduler_parasched_partition_staleness_seconds   full partition snapshot
#   scheduler_parasched_penalty_signal_age_seconds    lightweight conflict-rate feed
#
# This board produces no method comparison. Q_bind / ACF are collected only as a
# sanity check that the run behaved like its Board B/F counterpart.
#
# Safety conventions:
#   1. Dry-run by default: it only prints the configuration and the command it
#      would execute. Only --execute touches the cluster.
#   2. It preflights the Stage environment and the instrumented images, and
#      exits on the first unmet condition.
#   3. It calls process-results.py after the run. run-experiment.sh does not,
#      and skipping that step leaves the new metrics sitting in the metrics
#      JSON without ever reaching the summary record.
#   4. It verifies before exiting that the new fields actually landed, so the
#      board cannot silently produce nulls.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RESULTS_DIR="$PROJECT_ROOT/experiments/results"

# Fixed configuration. It matches the paper's B2-10000n-P4 run item by item and
# must not be changed casually: changing it here means H1 can no longer serve as
# a sanity check against the ParKour-P arm of Boards B and F.
NUM_NODES=10000
NUM_SCHEDULERS=10
CANDIDATE_K=2
PENALTY=0.5
STRATEGY="QualityFirst"
SYNC_PERIOD="1.0"
PARTITIONS=1
SYNC_PATTERN="glob"
PODS_PER_NODE=1
CPU_REQUEST="24000m"
MEMORY_REQUEST="192Gi"
VARIANCE="0.6"

TRIALS=3
RUN_ID=""
PROMETHEUS_URL="http://${MONITORING_IP:-<MONITORING_IP>}:9091"
MODE="dry-run"
SKIP_PREFLIGHT=false

die() { echo "[board-h] error: $*" >&2; exit 1; }
log() { echo "[board-h] $*"; }

usage() {
    cat <<'EOF'
Usage: run-board-h.sh [options]

  --execute               Actually run the experiment (default is a dry run
                          that only prints the configuration)
  --run-id NAME           Result directory prefix, default H1-freshness-<date>
  --trials N              Number of repetitions, default 3
  --prometheus-url URL    Prometheus address
  --skip-preflight        Skip the preflight checks (not recommended; for
                          troubleshooting only)
  -h, --help              Show this help

Preconditions (checked automatically under --execute):
  1. No leftover Board F Stage in the cluster; the default pod-ready Stage is
     in place.
  2. Schedulers run the instrumented image (partition_staleness already uses
     the 20-bucket scheme, and the penalty_signal_age metric exists).
  3. All para-system components are ready.

This board must run only after the formal matrices of the boards it depends on
have completed, and its images must use a traceable dedicated tag rather than
reusing "latest".
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute) MODE="execute"; shift ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --trials) TRIALS="$2"; shift 2 ;;
        --prometheus-url) PROMETHEUS_URL="$2"; shift 2 ;;
        --skip-preflight) SKIP_PREFLIGHT=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see -h for usage)" ;;
    esac
done

[[ -n "$RUN_ID" ]] || RUN_ID="H1-freshness-$(date +%Y%m%d)"

promq() {
    # Return the number of results of an instant PromQL query. On failure it
    # returns an empty string; the caller decides what that means.
    curl -sf -m 15 --get "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode "query=$1" 2>/dev/null \
        | jq -r '.data.result | length' 2>/dev/null || true
}

preflight() {
    log "preflight checks..."

    command -v kubectl >/dev/null || die "kubectl not found"
    command -v jq >/dev/null || die "jq not found"

    # 1. In this deployment a Stage is not a cluster CRD but the --config of the
    #    KWOK process:
    #      Z0 = kwok-config.yaml only (KWOK's built-in default Stage)
    #      F  = additionally appends pod-ready-dreal*.yaml
    #    So check the process arguments, not `kubectl get stages`.
    local kwok_pids shard_count cfg_count leftover_cfg pid
    kwok_pids=$(pgrep -x kwok 2>/dev/null || true)
    [[ -n "$kwok_pids" ]] || die "no running KWOK shard process"
    shard_count=$(printf '%s\n' "$kwok_pids" | grep -c . || true)
    [[ "$shard_count" -eq 10 ]] || die "found $shard_count KWOK shard processes, expected 10"

    # /proc/PID/cmdline is NUL-separated; reading argument by argument is more
    # reliable than parsing ps output.
    cfg_count=$(tr '\0' '\n' < "/proc/$(printf '%s\n' "$kwok_pids" | head -1)/cmdline" \
        | grep -c '^--config' || true)
    leftover_cfg=0
    for pid in $kwok_pids; do
        if tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'pod-ready-dreal'; then
            leftover_cfg=$((leftover_cfg + 1))
        fi
    done
    [[ "$leftover_cfg" -eq 0 ]] || die "$leftover_cfg KWOK shards still load the Board F Stage config; H1 requires Z0"
    [[ "$cfg_count" -eq 1 ]] || die "KWOK shards pass $cfg_count --config arguments, expected 1 (kwok-config.yaml only)"
    log "  Stage environment: clean (all 10 shards load only kwok-config.yaml and use the built-in default Stage)"

    # 2. para-system ready
    local not_ready
    not_ready=$(kubectl get pods -n para-system --no-headers 2>/dev/null \
        | grep -vc 'Running' || true)
    [[ "$not_ready" -eq 0 ]] || die "para-system has $not_ready pods not ready"
    log "  para-system: all Running"

    # 3. Instrumented image. The bucket count distinguishes old from new:
    #      old ExponentialBuckets(0.01, 2, 12)   -> 12 le + Inf = 13
    #      new ExponentialBuckets(0.01, 1.5, 20) -> 20 le + Inf = 21
    local metric_present n_signal
    metric_present=$(promq 'count(scheduler_parasched_partition_staleness_seconds_bucket)')
    if [[ -z "$metric_present" || "$metric_present" == "0" ]]; then
        die "Prometheus does not expose the partition_staleness metric; confirm the schedulers are up and $PROMETHEUS_URL is reachable"
    fi
    local le_count
    le_count=$(curl -sf -m 15 --get "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode 'query=count by (le) (scheduler_parasched_partition_staleness_seconds_bucket)' \
        | jq -r '.data.result | length')
    [[ "$le_count" -ge 15 ]] || die "partition_staleness has only $le_count buckets, still the old 12-bucket implementation.
       H1 needs the instrumented image with the new bucket scheme. If the image is known to be correct, pass --skip-preflight."
    log "  histogram buckets: $le_count (new bucket scheme)"

    n_signal=$(promq 'scheduler_parasched_penalty_signal_age_seconds_bucket')
    [[ -n "$n_signal" && "$n_signal" != "0" ]] || die "penalty_signal_age metric not found.
       It arrives with the instrumented image. If the schedulers have only just started and
       have not yet installed a conflict-rate table, retry shortly or pass --skip-preflight."
    log "  penalty_signal_age: exported ($n_signal series)"
}

print_config() {
    cat <<EOF

  Sub-experiment  H1 (synchronization-channel freshness)
  run-id          $RUN_ID
  Sync paradigm   periodic globSync, G=${SYNC_PERIOD}s, partitions=${PARTITIONS}
  Method          ParKour-P (K=${CANDIDATE_K}, w=${PENALTY}, ${STRATEGY})
  Data plane      Z0 (no injection; the data plane is not this board's variable)
  Scale           ${NUM_NODES} nodes / ${NUM_SCHEDULERS} schedulers / HC-V V=${VARIANCE}
  Workload        ${PODS_PER_NODE} pod/node, ${CPU_REQUEST} / ${MEMORY_REQUEST}
  Trials          ${TRIALS}
  Prometheus      ${PROMETHEUS_URL}

EOF
}

main() {
    print_config

    local -a cmd=(
        bash "$SCRIPT_DIR/run-experiment.sh"
        --name "$RUN_ID"
        --nodes "$NUM_NODES" --schedulers "$NUM_SCHEDULERS"
        --backup "$CANDIDATE_K" --penalty "$PENALTY" --strategy "$STRATEGY"
        --sync-period "$SYNC_PERIOD" --partitions "$PARTITIONS" --sync-pattern "$SYNC_PATTERN"
        --trials "$TRIALS"
        --pods-per-node "$PODS_PER_NODE"
        --cpu-request "$CPU_REQUEST" --memory-request "$MEMORY_REQUEST"
        --variance "$VARIANCE"
        --prometheus-url "$PROMETHEUS_URL"
        --collect-logs
    )

    if [[ "$MODE" != "execute" ]]; then
        log "dry run: the command below is not executed; pass --execute to run it"
        printf '  %q' "${cmd[@]}"; printf '\n\n'
        log "dry run finished (cluster not contacted, no experiment run)"
        return 0
    fi

    if [[ "$SKIP_PREFLIGHT" == true ]]; then
        log "preflight checks skipped (--skip-preflight)"
    else
        preflight
    fi

    log "running ${TRIALS} trials..."
    "${cmd[@]}"

    # run-experiment.sh appends its own timestamp, so pick the newest directory
    # matching the prefix afterwards.
    local exp_dir
    exp_dir=$(ls -1d "$RESULTS_DIR/${RUN_ID}"_* 2>/dev/null | sort | tail -1)
    [[ -n "$exp_dir" && -d "$exp_dir" ]] || die "result directory ${RESULTS_DIR}/${RUN_ID}_* not found"
    log "result directory: $exp_dir"

    # run-experiment.sh only calls collect-metrics.sh, not process-results.py.
    # Skipping this step keeps the new metrics out of the summary record.
    log "processing results..."
    python3 "$SCRIPT_DIR/process-results.py" "$exp_dir" --output "$exp_dir/board-h"

    verify_output "$exp_dir"
}

verify_output() {
    local exp_dir=$1 json="$1/board-h.json"
    [[ -f "$json" ]] || die "process-results.py did not produce $json"

    log "verifying that the new metrics landed..."
    local missing
    missing=$(jq -r '
        [ .[] | select((.penalty_signal_age_p50_ms == null)
                    or (.staleness_p50_ms == null)) ] | length' "$json")
    if [[ "$missing" != "0" ]]; then
        log "warning: $missing trials have null freshness fields."
        log "         Common causes: uninstrumented image, penalty disabled (w=0),"
        log "         or a missing PromQL query on the collection side."
    fi

    echo
    log "Installation age of the two synchronization channels (ms). Read these by bucket"
    log "membership; the interpolated values must not be quoted directly:"
    jq -r '["trial","staleness_p50","staleness_p99","signal_age_p50","signal_age_p99"],
           (.[] | [.trial // "-", .staleness_p50_ms, .staleness_p99_ms,
                   .penalty_signal_age_p50_ms, .penalty_signal_age_p99_ms])
           | @tsv' "$json" | column -t
    echo
    log "Done. See \"Board H\" in experiment-design.md for the decision rule."
}

main "$@"
