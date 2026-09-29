#!/bin/bash

# Collect metrics from Prometheus for Para-Sched experiments.
#
# Queries are organized by priority (P0/P1/P2) aligned with experiment-design.md §2.3.
# All results are saved as individual JSON files under the output directory.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9091}"
OUTPUT_DIR=""
START_TIME=""
END_TIME=""
STEP="15s"
SNAP_END_PAD=0   # extra seconds added to END_TIME for snapshot (counter/histogram) queries only

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --start)
            START_TIME="$2"
            shift 2
            ;;
        --end)
            END_TIME="$2"
            shift 2
            ;;
        --step)
            STEP="$2"
            shift 2
            ;;
        --snap-end-pad)
            SNAP_END_PAD="$2"
            shift 2
            ;;
        --prometheus-url)
            PROMETHEUS_URL="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 --output <dir> --start <time> --end <time> [options]"
            echo ""
            echo "Required:"
            echo "  --output DIR        Output directory for metric JSON files"
            echo "  --start TIME        Start time (Unix timestamp)"
            echo "  --end TIME          End time (Unix timestamp)"
            echo ""
            echo "Optional:"
            echo "  --step DURATION     Query step (default: 15s)"
            echo "  --snap-end-pad SECS Extra seconds added to END_TIME for snapshot"
            echo "                      (counter/histogram diff) queries only. Range and"
            echo "                      instant queries still use the original END_TIME,"
            echo "                      preserving resource-metric last-sample semantics."
            echo "                      Use this to avoid histogram sample deficit in"
            echo "                      short-duration experiments (default: 0)"
            echo "  --prometheus-url    Prometheus URL (default: http://localhost:9091)"
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
if [ -z "$OUTPUT_DIR" ] || [ -z "$START_TIME" ] || [ -z "$END_TIME" ]; then
    echo "Error: --output, --start, and --end are required"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Snapshot end-time (only for counter/histogram diff queries).
# Padding END_TIME lets Prometheus capture >= 2 extra scrapes of the saturation
# tail, which is critical for short trials where histogram buckets wouldn't
# otherwise accumulate enough samples for percentile computation.
SNAP_END=$((END_TIME + SNAP_END_PAD))

# If SNAP_END is in the future, wait for Prometheus to scrape it (+ 5s safety).
if [ "$SNAP_END_PAD" -gt 0 ]; then
    NOW=$(date +%s)
    if [ "$SNAP_END" -gt "$NOW" ]; then
        WAIT=$((SNAP_END - NOW + 5))
        echo "Waiting ${WAIT}s for Prometheus to scrape snapshot end-window..."
        sleep "$WAIT"
    fi
fi

echo "Collecting metrics from Prometheus..."
echo "  URL: $PROMETHEUS_URL"
echo "  Range/instant window: $START_TIME to $END_TIME (step=$STEP)"
if [ "$SNAP_END_PAD" -gt 0 ]; then
    echo "  Snapshot end:         $SNAP_END (+${SNAP_END_PAD}s pad)"
fi
echo "  Output dir: $OUTPUT_DIR"

# Save collection metadata
cat > "$OUTPUT_DIR/meta.json" <<EOF
{
  "collection_time": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "prometheus_url": "$PROMETHEUS_URL",
  "time_range": {"start": "$START_TIME", "end": "$END_TIME", "step": "$STEP"},
  "snapshot_end": "$SNAP_END",
  "snap_end_pad_seconds": $SNAP_END_PAD
}
EOF

# ---- Helper: query Prometheus range API and save result ----
query_metric() {
    local name=$1
    local query=$2
    echo "  [$name]"
    curl -sf -G "$PROMETHEUS_URL/api/v1/query_range" \
        --data-urlencode "query=$query" \
        --data-urlencode "start=$START_TIME" \
        --data-urlencode "end=$END_TIME" \
        --data-urlencode "step=$STEP" \
        -o "$OUTPUT_DIR/${name}.json" 2>/dev/null || echo "    WARN: query failed"
}

# ---- Helper: instant query (for cumulative counters at experiment end) ----
query_instant() {
    local name=$1
    local query=$2
    echo "  [$name] (instant)"
    curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode "query=$query" \
        --data-urlencode "time=$END_TIME" \
        -o "$OUTPUT_DIR/${name}.json" 2>/dev/null || echo "    WARN: query failed"
}

# ==============================================================
#  P0: Core metrics (experiment-design.md §2.3 — required for key results)
# ==============================================================
echo ""
echo "=== P0: Core metrics ==="

# All scheduler_* metrics are filtered by instance=~"sched-.*" to exclude
# the default kube-scheduler.  Without this filter, histogram queries aggregate
# data from both schedulers, causing two artifacts:
#   6.2: e2e_p99 ≈ 71,500ms (default-scheduler's empty histogram pollutes quantile)
#   6.3: algo_p99 ≈ 2.0ms in Trial 2 (stale default-scheduler histogram
#        contaminates snapshot diff after para-scheduler pod restarts)
SCHED_FILTER='instance=~"sched-.*"'

# Scheduling throughput (pods/s) — from kube-scheduler built-in
query_metric "throughput" \
    "sum(rate(scheduler_schedule_attempts_total{result=\"scheduled\",${SCHED_FILTER}}[1m]))"

# Per-scheduler throughput breakdown
query_metric "throughput_per_scheduler" \
    "rate(scheduler_schedule_attempts_total{result=\"scheduled\",${SCHED_FILTER}}[1m])"

# E2E scheduling latency P50/P99 — K8s 1.33 uses scheduler_pod_scheduling_sli_duration_seconds
query_metric "e2e_latency_p50" \
    "histogram_quantile(0.50, sum(rate(scheduler_pod_scheduling_sli_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

query_metric "e2e_latency_p99" \
    "histogram_quantile(0.99, sum(rate(scheduler_pod_scheduling_sli_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

# Scheduling algorithm duration P50/P99 — kube-scheduler built-in
query_metric "algo_latency_p50" \
    "histogram_quantile(0.50, sum(rate(scheduler_scheduling_algorithm_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

query_metric "algo_latency_p99" \
    "histogram_quantile(0.99, sum(rate(scheduler_scheduling_algorithm_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

# Conflict rate — from Binder low-cardinality metric (parasched_bind_result_total)
query_metric "conflict_rate" \
    'sum(rate(parasched_bind_result_total{result="conflict"}[1m])) / sum(rate(parasched_bind_result_total[1m]))'

# Binder bind duration P50/P99
query_metric "bind_latency_p50" \
    'histogram_quantile(0.50, sum(rate(parasched_bind_duration_seconds_bucket[1m])) by (le))'

query_metric "bind_latency_p99" \
    'histogram_quantile(0.99, sum(rate(parasched_bind_duration_seconds_bucket[1m])) by (le))'

# Pod scheduling attempts distribution (how many attempts before success)
query_metric "pod_scheduling_attempts_p50" \
    "histogram_quantile(0.50, sum(rate(scheduler_pod_scheduling_attempts_bucket{${SCHED_FILTER}}[1m])) by (le))"

query_metric "pod_scheduling_attempts_p99" \
    "histogram_quantile(0.99, sum(rate(scheduler_pod_scheduling_attempts_bucket{${SCHED_FILTER}}[1m])) by (le))"

# Per-window totals using increase() to handle counter resets across trials.
# These represent the total count *within this experiment phase*, not the raw
# cumulative counter (which resets when pods restart between trials).
DURATION=$((END_TIME - START_TIME))

query_instant "total_scheduled" \
    "sum(increase(scheduler_schedule_attempts_total{result=\"scheduled\",${SCHED_FILTER}}[${DURATION}s]))"

query_instant "total_unschedulable" \
    "sum(increase(scheduler_schedule_attempts_total{result=\"unschedulable\",${SCHED_FILTER}}[${DURATION}s]))"

query_instant "total_bind_conflicts" \
    "sum(increase(parasched_bind_result_total{result=\"conflict\"}[${DURATION}s]))"

query_instant "total_bind_success" \
    "sum(increase(parasched_bind_result_total{result=\"success\"}[${DURATION}s]))"

query_instant "total_all_candidates_failed" \
    "sum(increase(parasched_all_candidates_failed_total[${DURATION}s]))"

# Counter-delta throughput using Prometheus increase() to handle counter resets.
# increase() automatically detects counter resets (e.g. pod restarts between trials)
# and computes the correct delta, unlike raw instant-query subtraction.
echo "  [throughput_delta] (increase-based)"

# Helper: query increase() over the experiment window via instant query at end time.
# increase(metric[Xs]) where X = window duration, evaluated at end time.
query_increase() {
    local query=$1
    curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode "query=${query}" \
        --data-urlencode "time=$END_TIME" 2>/dev/null \
        | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '0')" 2>/dev/null || echo "0"
}

SCHED_INCREASE=$(query_increase "sum(increase(scheduler_schedule_attempts_total{result=\"scheduled\",${SCHED_FILTER}}[${DURATION}s]))")
BIND_INCREASE=$(query_increase "sum(increase(parasched_bind_result_total{result=\"success\"}[${DURATION}s]))")
CONFLICT_INCREASE=$(query_increase "sum(increase(parasched_bind_result_total{result=\"conflict\"}[${DURATION}s]))")

cat > "$OUTPUT_DIR/throughput_delta.json" <<DELTA
{
  "duration_seconds": $DURATION,
  "scheduler_scheduled_delta": $(python3 -c "print(int(round(float('${SCHED_INCREASE}'))))" 2>/dev/null || echo 0),
  "scheduler_throughput_pods_per_sec": $(python3 -c "d=float('${SCHED_INCREASE}'); print(round(d/$DURATION, 2) if $DURATION>0 else 0)" 2>/dev/null || echo 0),
  "binder_success_delta": $(python3 -c "print(int(round(float('${BIND_INCREASE}'))))" 2>/dev/null || echo 0),
  "binder_throughput_pods_per_sec": $(python3 -c "d=float('${BIND_INCREASE}'); print(round(d/$DURATION, 2) if $DURATION>0 else 0)" 2>/dev/null || echo 0),
  "binder_conflict_delta": $(python3 -c "print(int(round(float('${CONFLICT_INCREASE}'))))" 2>/dev/null || echo 0),
  "conflict_rate": $(python3 -c "
s=float('${BIND_INCREASE}')
c=float('${CONFLICT_INCREASE}')
print(round(c/(s+c), 4) if (s+c)>0 else 0)" 2>/dev/null || echo 0)
}
DELTA
echo "    scheduler: $(cat "$OUTPUT_DIR/throughput_delta.json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['scheduler_scheduled_delta']} pods / {d['duration_seconds']}s = {d['scheduler_throughput_pods_per_sec']} pods/s\")")"
echo "    binder:    $(cat "$OUTPUT_DIR/throughput_delta.json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['binder_success_delta']} success, {d['binder_conflict_delta']} conflicts, rate={d['conflict_rate']}\")")"

# ==============================================================
#  P1: Resource and quality metrics
# ==============================================================
echo ""
echo "=== P1: Resource & quality metrics ==="

# Candidate rank accepted distribution (which candidate was ultimately bound)
query_metric "candidate_rank_p50" \
    'histogram_quantile(0.50, sum(rate(parasched_candidate_rank_accepted_bucket[1m])) by (le))'

query_metric "candidate_rank_p99" \
    'histogram_quantile(0.99, sum(rate(parasched_candidate_rank_accepted_bucket[1m])) by (le))'

# Selected node score distribution
query_metric "selected_node_score_p50" \
    'histogram_quantile(0.50, sum(rate(scheduler_parasched_selected_node_score_bucket[1m])) by (le))'

# Scheduler CPU usage (per container on Node B)
# Filter by pod=~"para-scheduler-.*" to exclude kube-scheduler and leftover pods
# from previous experiments. The container name "scheduler" alone is not unique —
# kube-scheduler also uses it, and stale pods from prior A3 runs (with different
# NUM_SCHEDULERS) may still be scraped by Prometheus.
query_metric "scheduler_cpu" \
    'rate(container_cpu_usage_seconds_total{container="scheduler",namespace="para-system",pod=~"para-scheduler-.*"}[1m])'

# Scheduler memory RSS
query_metric "scheduler_memory_rss" \
    'container_memory_rss{container="scheduler",namespace="para-system",pod=~"para-scheduler-.*"}'

# Binder CPU/memory
query_metric "binder_cpu" \
    'rate(container_cpu_usage_seconds_total{container="binder",namespace="para-system",pod=~"para-binder-.*"}[1m])'

query_metric "binder_memory_rss" \
    'container_memory_rss{container="binder",namespace="para-system",pod=~"para-binder-.*"}'

# Dispatcher CPU/memory
query_metric "dispatcher_cpu" \
    'rate(container_cpu_usage_seconds_total{container="dispatcher",namespace="para-system",pod=~"para-dispatcher-.*"}[1m])'

query_metric "dispatcher_memory_rss" \
    'container_memory_rss{container="dispatcher",namespace="para-system",pod=~"para-dispatcher-.*"}'

# ==============================================================
#  P1: Dispatcher metrics
# ==============================================================
echo ""
echo "=== P1: Dispatcher metrics ==="

# Dispatch throughput (pods/s)
query_metric "dispatch_throughput" \
    'sum(rate(parasched_dispatch_total{result="success"}[1m]))'

# Dispatch latency P50/P99
query_metric "dispatch_latency_p50" \
    'histogram_quantile(0.50, sum(rate(parasched_dispatch_duration_seconds_bucket[1m])) by (le))'

query_metric "dispatch_latency_p99" \
    'histogram_quantile(0.99, sum(rate(parasched_dispatch_duration_seconds_bucket[1m])) by (le))'

# Dispatcher queue depth
query_metric "dispatch_queue_depth" \
    'parasched_dispatcher_queue_depth'

# Scheduler load balance (inflight pods per scheduler)
query_metric "scheduler_inflight" \
    'parasched_dispatcher_scheduler_inflight'

# ==============================================================
#  P2: ParSync & microbenchmark metrics
# ==============================================================
echo ""
echo "=== P2: ParSync & microbenchmark metrics ==="

# Candidate selection duration (microbenchmark)
query_metric "candidate_selection_p50" \
    'histogram_quantile(0.50, sum(rate(scheduler_parasched_candidate_selection_duration_seconds_bucket[1m])) by (le))'

query_metric "candidate_selection_p99" \
    'histogram_quantile(0.99, sum(rate(scheduler_parasched_candidate_selection_duration_seconds_bucket[1m])) by (le))'

# Penalty lookup duration
query_metric "penalty_lookup_p50" \
    'histogram_quantile(0.50, sum(rate(scheduler_parasched_penalty_lookup_duration_seconds_bucket[1m])) by (le))'

# ParSync sync duration P50/P99
query_metric "sync_duration_p50" \
    'histogram_quantile(0.50, sum(rate(scheduler_parasched_sync_duration_seconds_bucket[1m])) by (le))'

query_metric "sync_duration_p99" \
    'histogram_quantile(0.99, sum(rate(scheduler_parasched_sync_duration_seconds_bucket[1m])) by (le))'

# Partition staleness P50/P99
query_metric "partition_staleness_p50" \
    'histogram_quantile(0.50, sum(rate(scheduler_parasched_partition_staleness_seconds_bucket[1m])) by (le))'

query_metric "partition_staleness_p99" \
    'histogram_quantile(0.99, sum(rate(scheduler_parasched_partition_staleness_seconds_bucket[1m])) by (le))'

# Conflict-rate feed age P50/P99. Shares buckets with partition_staleness so the
# two delivery pipelines (lightweight penalty feed vs full snapshot) can be
# compared directly. Only non-empty when the penalty mechanism is enabled (w>0).
query_metric "penalty_signal_age_p50" \
    'histogram_quantile(0.50, sum(rate(scheduler_parasched_penalty_signal_age_seconds_bucket[1m])) by (le))'

query_metric "penalty_signal_age_p99" \
    'histogram_quantile(0.99, sum(rate(scheduler_parasched_penalty_signal_age_seconds_bucket[1m])) by (le))'

# All candidates failed rate
query_metric "all_candidates_failed_rate" \
    'rate(parasched_all_candidates_failed_total[1m])'

# ---- Cold-start observability ----

# First snapshot applied per partition. The latest of these across partitions
# minus the experiment start time = cold-start duration. Instant query at end
# captures the gauge value (it's only set once per partition).
query_instant "first_snapshot_applied" \
    'scheduler_parasched_first_snapshot_applied_timestamp_seconds'

# Assumed pod count gauge — sampled over the experiment window. P99 of this
# series indicates whether assumed pods are accumulating (TTL bug / nodes with
# partitionID=-1 retaining state forever).
query_metric "assumed_pod_count" \
    'scheduler_parasched_assumed_pod_count'

# Dispatcher readiness timestamp. Validates that the readinessProbe + initContainer
# (or kubectl wait) gate actually held off Scheduler/Binder until partition
# labels were assigned.
query_instant "dispatcher_ready_at" \
    'parasched_dispatcher_ready_timestamp_seconds'

# Snapshot publish chunks (P2): per-partition cumulative chunk count. With
# 5000 nodes/chunk, P=1 / 10000-node experiments should see this climb in
# increments of 2 per heartbeat. Used to verify chunking is actually firing.
query_metric "snapshot_publish_chunks" \
    'parasched_snapshot_publish_chunks_total'

# Snapshot oversize counter — reserved for the (currently fatal) path where a
# single chunk exceeds the 1 MiB ConfigMap limit after chunking. Stays 0 unless
# the fatal handling in publishChunks is relaxed.
query_instant "snapshot_oversize_total" \
    'sum(parasched_snapshot_oversize_total)'

# Snapshot publish errors — transient ConfigMap Update/Create failures broken
# down by reason ("throttled","conflict","timeout","other"). Retried via
# re-marking the partition dirty, so isolated events are tolerable; a sustained
# non-zero rate indicates client-side QPS throttling or apiserver pressure.
query_instant "snapshot_publish_errors_total" \
    'sum(parasched_snapshot_publish_errors_total) by (reason)'

# ==============================================================
#  Counter-snapshot diff: scrape-interval-independent metrics
# ==============================================================
# When experiment duration < Prometheus scrape_interval (15s), rate()/increase()
# queries return empty because they need ≥2 data points.  The snapshot diff
# approach queries the raw counter values at START and END via instant queries,
# then computes the delta in Python.  This works as long as Prometheus has
# stored at least one sample before START and one after END (which is almost
# always true since scraped data lives in TSDB).
#
# For histograms, we snapshot all _bucket/_sum/_count series and compute
# percentiles from the bucket deltas.

echo ""
echo "=== Snapshot diff: scrape-interval-independent ==="

# Helper: instant query at a specific time, return raw JSON to stdout
query_at() {
    local query=$1 ts=$2
    curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode "query=$query" \
        --data-urlencode "time=$ts" 2>/dev/null
}

# Helper: query a counter at both endpoints, compute per-label delta via Python.
# Uses temp files to avoid shell quoting issues with JSON in heredocs.
snapshot_counter() {
    local name=$1 query=$2
    echo "  [${name}] (snapshot diff)"
    local pre_f=$(mktemp) post_f=$(mktemp)
    query_at "$query" "$START_TIME" > "$pre_f"  || echo '{"data":{"result":[]}}' > "$pre_f"
    query_at "$query" "$SNAP_END"   > "$post_f" || echo '{"data":{"result":[]}}' > "$post_f"
    local snap_dur=$((SNAP_END - START_TIME))
    python3 - "$pre_f" "$post_f" "$name" "$snap_dur" <<'PYEOF' > "$OUTPUT_DIR/${name}.json" 2>/dev/null || echo "    WARN: snapshot diff failed"
import json, sys
pre_raw  = json.load(open(sys.argv[1]))
post_raw = json.load(open(sys.argv[2]))
name, dur = sys.argv[3], int(sys.argv[4])
def to_map(raw):
    m = {}
    for r in raw.get('data',{}).get('result',[]):
        key = json.dumps(r['metric'], sort_keys=True)
        m[key] = float(r['value'][1])
    return m
pre_m, post_m = to_map(pre_raw), to_map(post_raw)
results = []
for key in post_m:
    delta = post_m[key] - pre_m.get(key, 0)
    if delta < 0: delta = post_m[key]  # counter reset
    results.append({'metric': json.loads(key), 'delta': delta})
json.dump({'name': name, 'duration': dur, 'results': results}, sys.stdout, indent=2)
PYEOF
    rm -f "$pre_f" "$post_f"
}

# Helper: snapshot a histogram (all buckets + sum + count), compute percentiles.
# Uses temp files to avoid shell quoting issues with JSON.
# metric_base can include label selectors, e.g.:
#   "scheduler_scheduling_algorithm_duration_seconds{profile=\"para-scheduler\"}"
# The function splits it into metric name + selector, then appends _bucket/_sum/_count
# before the selector: metric_name_bucket{selector}
snapshot_histogram() {
    local name=$1 metric_base=$2
    echo "  [${name}] (snapshot histogram diff)"

    # Split metric_base into metric name and label selector.
    # "foo{bar=baz}" → metric_name="foo", selector="{bar=baz}"
    # "foo"          → metric_name="foo", selector=""
    local metric_name="${metric_base%%\{*}"
    local selector=""
    if [[ "$metric_base" == *"{"* ]]; then
        selector="{${metric_base#*\{}"
    fi

    local tmpdir=$(mktemp -d)
    query_at "sum by (le) (${metric_name}_bucket${selector})" "$START_TIME" > "$tmpdir/pre_b.json"  || echo '{}' > "$tmpdir/pre_b.json"
    query_at "sum by (le) (${metric_name}_bucket${selector})" "$SNAP_END"   > "$tmpdir/post_b.json" || echo '{}' > "$tmpdir/post_b.json"
    query_at "sum(${metric_name}_sum${selector})"   "$START_TIME" > "$tmpdir/pre_s.json"  || echo '{}' > "$tmpdir/pre_s.json"
    query_at "sum(${metric_name}_sum${selector})"   "$SNAP_END"   > "$tmpdir/post_s.json" || echo '{}' > "$tmpdir/post_s.json"
    query_at "sum(${metric_name}_count${selector})" "$START_TIME" > "$tmpdir/pre_c.json"  || echo '{}' > "$tmpdir/pre_c.json"
    query_at "sum(${metric_name}_count${selector})" "$SNAP_END"   > "$tmpdir/post_c.json" || echo '{}' > "$tmpdir/post_c.json"

    local snap_dur=$((SNAP_END - START_TIME))
    python3 - "$tmpdir" "$name" "$snap_dur" <<'PYEOF' > "$OUTPUT_DIR/${name}.json" 2>/dev/null || echo "    WARN: snapshot histogram diff failed"
import json, sys, math, os

tmpdir, name, dur = sys.argv[1], sys.argv[2], int(sys.argv[3])

def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {'data': {'result': []}}

def parse_val(raw):
    results = raw.get('data', {}).get('result', [])
    return float(results[0]['value'][1]) if results else None

def parse_buckets(raw):
    buckets = {}
    for item in raw.get('data', {}).get('result', []):
        le = item['metric'].get('le', '')
        buckets[le] = float(item['value'][1])
    return buckets

def bucket_diff(pre, post):
    diff = {}
    for le in post:
        d = post[le] - pre.get(le, 0)
        if d < 0: d = post[le]
        diff[le] = d
    return diff

def percentile(buckets, q):
    items = []
    for le, count in buckets.items():
        if le == '+Inf':
            items.append((float('inf'), count))
        else:
            try: items.append((float(le), count))
            except: pass
    items.sort(key=lambda x: x[0])
    if not items:
        return None
    total = items[-1][1]
    if total == 0:
        return None
    target = q * total
    prev_le, prev_count = 0, 0
    for le, count in items:
        if count >= target:
            if count == prev_count:
                return le
            fraction = (target - prev_count) / (count - prev_count)
            return prev_le + fraction * (le - prev_le) if not math.isinf(le) else prev_le
        prev_le, prev_count = le, count
    return items[-2][0] if len(items) >= 2 else None

pre_b  = parse_buckets(load(os.path.join(tmpdir, 'pre_b.json')))
post_b = parse_buckets(load(os.path.join(tmpdir, 'post_b.json')))
pre_sum_v  = parse_val(load(os.path.join(tmpdir, 'pre_s.json')))
post_sum_v = parse_val(load(os.path.join(tmpdir, 'post_s.json')))
pre_cnt_v  = parse_val(load(os.path.join(tmpdir, 'pre_c.json')))
post_cnt_v = parse_val(load(os.path.join(tmpdir, 'post_c.json')))

diff_b = bucket_diff(pre_b, post_b)
diff_sum   = (post_sum_v or 0) - (pre_sum_v or 0)
diff_count = (post_cnt_v or 0) - (pre_cnt_v or 0)
if diff_sum < 0: diff_sum = post_sum_v or 0
if diff_count < 0: diff_count = post_cnt_v or 0

p50 = percentile(diff_b, 0.50)
p99 = percentile(diff_b, 0.99)
avg = diff_sum / diff_count if diff_count > 0 else None

json.dump({
    'name': name, 'duration': dur,
    'count': diff_count, 'sum': round(diff_sum, 6),
    'avg': round(avg, 6) if avg else None,
    'p50': round(p50, 6) if p50 else None,
    'p99': round(p99, 6) if p99 else None,
}, sys.stdout, indent=2)
PYEOF
    rm -rf "$tmpdir"
}

# ---- Counters ----
# All scheduler_* counters use SCHED_FILTER to exclude default-scheduler.
snapshot_counter "snap_scheduled" \
    "scheduler_schedule_attempts_total{result=\"scheduled\",${SCHED_FILTER}}"

snapshot_counter "snap_unschedulable" \
    "scheduler_schedule_attempts_total{result=\"unschedulable\",${SCHED_FILTER}}"

snapshot_counter "snap_error" \
    "scheduler_schedule_attempts_total{result=\"error\",${SCHED_FILTER}}"

snapshot_counter "snap_bind_success" \
    'parasched_bind_result_total{result="success"}'

snapshot_counter "snap_bind_conflict" \
    'parasched_bind_result_total{result="conflict"}'

snapshot_counter "snap_all_candidates_failed" \
    'parasched_all_candidates_failed_total'

snapshot_counter "snap_dispatch_success" \
    'parasched_dispatch_total{result="success"}'

snapshot_counter "snap_dispatch_error" \
    'parasched_dispatch_total{result="error"}'

# ---- Histograms ----
# scheduler_* histograms filtered by SCHED_FILTER; parasched_* are para-sched-only.
snapshot_histogram "snap_e2e_latency" \
    "scheduler_pod_scheduling_sli_duration_seconds{${SCHED_FILTER}}"

snapshot_histogram "snap_algo_latency" \
    "scheduler_scheduling_algorithm_duration_seconds{${SCHED_FILTER}}"

snapshot_histogram "snap_bind_latency" \
    'parasched_bind_duration_seconds'

snapshot_histogram "snap_dispatch_latency" \
    'parasched_dispatch_duration_seconds'

snapshot_histogram "snap_pod_attempts" \
    "scheduler_pod_scheduling_attempts{${SCHED_FILTER}}"

snapshot_histogram "snap_candidate_selection" \
    'scheduler_parasched_candidate_selection_duration_seconds'

# ---- Derived summary ----
# Use the snapshot window duration (may include end-pad) since the counters/
# histograms it summarizes were queried at SNAP_END.
echo "  [snap_summary] (derived)"
python3 -c "
import json, os, sys

d = '$OUTPUT_DIR'
duration = $((SNAP_END - START_TIME))

def load(name):
    try:
        with open(os.path.join(d, name + '.json')) as f:
            return json.load(f)
    except: return None

def sum_delta(data):
    if not data or 'results' not in data: return 0
    return sum(r['delta'] for r in data['results'])

sched   = sum_delta(load('snap_scheduled'))
unsched = sum_delta(load('snap_unschedulable'))
err     = sum_delta(load('snap_error'))
bind_ok = sum_delta(load('snap_bind_success'))
bind_cf = sum_delta(load('snap_bind_conflict'))
acf     = sum_delta(load('snap_all_candidates_failed'))

e2e     = load('snap_e2e_latency')
algo    = load('snap_algo_latency')
bind_l  = load('snap_bind_latency')

summary = {
    'duration_seconds': duration,
    'scheduling': {
        'scheduled': sched,
        'unschedulable': unsched,
        'error': err,
        'throughput_pods_per_sec': round(sched / duration, 2) if duration > 0 else 0,
    },
    'binding': {
        'success': bind_ok,
        'conflict': bind_cf,
        'all_candidates_failed': acf,
        'conflict_rate': round(bind_cf / (bind_ok + bind_cf), 4) if (bind_ok + bind_cf) > 0 else 0,
    },
    'latency': {
        'e2e_p50':  e2e.get('p50')  if e2e else None,
        'e2e_p99':  e2e.get('p99')  if e2e else None,
        'e2e_avg':  e2e.get('avg')  if e2e else None,
        'algo_p50': algo.get('p50') if algo else None,
        'algo_p99': algo.get('p99') if algo else None,
        'bind_p50': bind_l.get('p50') if bind_l else None,
        'bind_p99': bind_l.get('p99') if bind_l else None,
    },
}
json.dump(summary, sys.stdout, indent=2)
print()  # trailing newline
" > "$OUTPUT_DIR/snap_summary.json" 2>/dev/null || echo "    WARN: summary generation failed"

# Print key results
if [ -f "$OUTPUT_DIR/snap_summary.json" ]; then
    python3 -c "
import json
with open('$OUTPUT_DIR/snap_summary.json') as f:
    s = json.load(f)
sch = s['scheduling']
bnd = s['binding']
lat = s['latency']
print(f'    scheduled={sch[\"scheduled\"]}, throughput={sch[\"throughput_pods_per_sec\"]} pods/s')
print(f'    conflicts={bnd[\"conflict\"]}, conflict_rate={bnd[\"conflict_rate\"]}, acf={bnd[\"all_candidates_failed\"]}')
e2e_p99 = f'{lat[\"e2e_p99\"]*1000:.1f}ms' if lat.get('e2e_p99') else 'N/A'
print(f'    e2e_latency_p99={e2e_p99}')
" 2>/dev/null || true
fi

# ==============================================================
#  Scheduling quality (saturation phase only)
# ==============================================================
# The selected-node score and accepted-candidate rank histograms, which the
# ablation figure reads. pull-quality-metrics.py snapshots them over the window
# meta.json records; running it here, while Prometheus still holds the run,
# leaves the trial's raw results complete when the trial ends.
if [ "$(basename "$OUTPUT_DIR")" = "metrics-saturation" ]; then
    echo "  [quality] (histogram snapshots)"
    python3 "$SCRIPT_DIR/pull-quality-metrics.py" "$(dirname "$OUTPUT_DIR")" \
        || echo "    WARN: quality metrics not captured"
fi

echo ""
echo "============================================"
echo "Metrics collected: $(ls "$OUTPUT_DIR"/*.json | wc -l) files"
echo "Output dir: $OUTPUT_DIR"
echo "============================================"
