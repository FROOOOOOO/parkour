#!/bin/bash

# Collect metrics from Prometheus for Godel baseline experiments (E1 / E2).
#
# Mirrors the P0 / P1-resource portions of collect-metrics.sh but rewrites the
# PromQL to use Godel's actual metric names (verified against a 50-pod
# activation run before the first full experiment):
#
#   scheduler_schedule_attempts_total          → scheduler_pod_scheduling_attempts (no _total)
#   scheduler_pod_scheduling_sli_duration_*    → scheduler_e2e_scheduling_duration_seconds
#   scheduler_scheduling_algorithm_duration_*  → unchanged
#   parasched_bind_result_total{result=...}    → binder_binding_pod_attempts{result=...}
#                                                (success → "bound", conflict → "unschedulable")
#   parasched_bind_duration_seconds            → binder_e2e_duration_seconds
#
# Output filenames are kept identical to collect-metrics.sh so downstream
# process-results.py / plot scripts work unchanged when pointed at the godel
# results directory.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
# Bypass HTTP(S) proxy for Prometheus + cluster traffic.
export_cluster_no_proxy

PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9091}"
OUTPUT_DIR=""
START_TIME=""
END_TIME=""
STEP="15s"
SNAP_END_PAD=0

while [[ $# -gt 0 ]]; do
    case $1 in
        --output)          OUTPUT_DIR="$2";          shift 2 ;;
        --start)           START_TIME="$2";          shift 2 ;;
        --end)             END_TIME="$2";            shift 2 ;;
        --step)            STEP="$2";                shift 2 ;;
        --snap-end-pad)    SNAP_END_PAD="$2";        shift 2 ;;
        --prometheus-url)  PROMETHEUS_URL="$2";      shift 2 ;;
        -h|--help)
            cat <<EOF
Usage: $0 --output <dir> --start <unix-ts> --end <unix-ts> [options]
  --step DURATION     Query step (default: 15s)
  --snap-end-pad SECS Extra seconds for counter/histogram diff queries (default: 0)
  --prometheus-url    Prometheus URL (default: http://localhost:9091)
EOF
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$OUTPUT_DIR" ] || [ -z "$START_TIME" ] || [ -z "$END_TIME" ]; then
    echo "Error: --output, --start, --end are required"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
SNAP_END=$((END_TIME + SNAP_END_PAD))

if [ "$SNAP_END_PAD" -gt 0 ]; then
    NOW=$(date +%s)
    if [ "$SNAP_END" -gt "$NOW" ]; then
        WAIT=$((SNAP_END - NOW + 5))
        echo "Waiting ${WAIT}s for Prometheus to scrape snapshot end-window..."
        sleep "$WAIT"
    fi
fi

echo "Collecting Godel metrics from Prometheus..."
echo "  URL: $PROMETHEUS_URL"
echo "  Range/instant window: $START_TIME to $END_TIME (step=$STEP)"
[ "$SNAP_END_PAD" -gt 0 ] && echo "  Snapshot end:         $SNAP_END (+${SNAP_END_PAD}s pad)"
echo "  Output dir: $OUTPUT_DIR"

cat > "$OUTPUT_DIR/meta.json" <<EOF
{
  "collection_time": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "prometheus_url": "$PROMETHEUS_URL",
  "baseline": "godel",
  "time_range": {"start": "$START_TIME", "end": "$END_TIME", "step": "$STEP"},
  "snapshot_end": "$SNAP_END",
  "snap_end_pad_seconds": $SNAP_END_PAD
}
EOF

# ---- query helpers (identical to collect-metrics.sh) ----
query_metric() {
    local name=$1 query=$2
    echo "  [$name]"
    curl -sf -G "$PROMETHEUS_URL/api/v1/query_range" \
        --data-urlencode "query=$query" \
        --data-urlencode "start=$START_TIME" \
        --data-urlencode "end=$SNAP_END" \
        --data-urlencode "step=$STEP" \
        -o "$OUTPUT_DIR/${name}.json" 2>/dev/null || echo "    WARN: query failed"
}

query_instant() {
    local name=$1 query=$2
    echo "  [$name] (instant)"
    curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode "query=$query" \
        --data-urlencode "time=$SNAP_END" \
        -o "$OUTPUT_DIR/${name}.json" 2>/dev/null || echo "    WARN: query failed"
}

query_increase() {
    local query=$1
    curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode "query=${query}" \
        --data-urlencode "time=$SNAP_END" 2>/dev/null \
        | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '0')" 2>/dev/null \
        || echo "0"
}

# Filter: prometheus.yml tags all godel scrape jobs with baseline="godel".
# Component label further narrows scheduler vs binder vs dispatcher.
SCHED_FILTER='baseline="godel",component="scheduler"'
BINDER_FILTER='baseline="godel",component="binder"'

# ==============================================================
#  P0: Core metrics
# ==============================================================
echo ""
echo "=== P0: Core metrics ==="

# Throughput (pods/s) — scheduler.scheduled counter rate
query_metric "throughput" \
    "sum(rate(scheduler_pod_scheduling_attempts{result=\"scheduled\",${SCHED_FILTER}}[1m]))"

query_metric "throughput_per_scheduler" \
    "rate(scheduler_pod_scheduling_attempts{result=\"scheduled\",${SCHED_FILTER}}[1m])"

# E2E scheduling latency (Godel's own scheduler_e2e_scheduling_duration_seconds)
query_metric "e2e_latency_p50" \
    "histogram_quantile(0.50, sum(rate(scheduler_e2e_scheduling_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

query_metric "e2e_latency_p99" \
    "histogram_quantile(0.99, sum(rate(scheduler_e2e_scheduling_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

# Scheduling algorithm duration (same metric name as K8s native)
query_metric "algo_latency_p50" \
    "histogram_quantile(0.50, sum(rate(scheduler_scheduling_algorithm_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

query_metric "algo_latency_p99" \
    "histogram_quantile(0.99, sum(rate(scheduler_scheduling_algorithm_duration_seconds_bucket{${SCHED_FILTER}}[1m])) by (le))"

# Conflict rate — uses binder_pod_binding_failure{reason="check_conflicts"} which
# is incremented in AddFailedTask() when CheckConflictPhase returns Unschedule/Error
# (see godel-scheduler/pkg/binder/binder_unit.go:587-605).
# NOTE: binder_check_conflict_failure_count is DEAD — CheckConflictFailuresInc is
# registered but never called anywhere in the codebase.
#
# NOTE: binder_binding_pod_attempts uses result="success"/"failure" (not "bound"/
# "unschedulable" as the HELP text claims — source values come from
# pkg/binder/metrics/consts.go:52,55).
query_metric "conflict_rate" \
    "sum(rate(binder_pod_binding_failure{reason=\"check_conflicts_evaluation\",${BINDER_FILTER}}[1m])) / sum(rate(binder_binding_pod_attempts{${BINDER_FILTER}}[1m]))"

# Binder e2e bind duration
query_metric "bind_latency_p50" \
    "histogram_quantile(0.50, sum(rate(binder_e2e_duration_seconds_bucket{${BINDER_FILTER}}[1m])) by (le))"

query_metric "bind_latency_p99" \
    "histogram_quantile(0.99, sum(rate(binder_e2e_duration_seconds_bucket{${BINDER_FILTER}}[1m])) by (le))"

# Per-window totals using increase().
# DURATION = actual saturation window (no snap-end padding) — this is the
# throughput divisor and the increase() window for all counter queries.
# SNAP_END is only the query timestamp for counter/histogram snapshots.
DURATION=$((END_TIME - START_TIME))

query_instant "total_scheduled" \
    "sum(increase(scheduler_pod_scheduling_attempts{result=\"scheduled\",${SCHED_FILTER}}[${DURATION}s]))"

query_instant "total_unschedulable" \
    "sum(increase(scheduler_pod_scheduling_attempts{result=\"unschedulable\",${SCHED_FILTER}}[${DURATION}s]))"

query_instant "total_bind_success" \
    "sum(increase(binder_binding_pod_attempts{result=\"success\",${BINDER_FILTER}}[${DURATION}s]))"

query_instant "total_bind_failures" \
    "sum(increase(binder_binding_pod_attempts{result=\"failure\",${BINDER_FILTER}}[${DURATION}s]))"

# Conflict failures — resource conflicts detected by binder (reason="check_conflicts").
query_instant "total_bind_conflicts" \
    "sum(increase(binder_pod_binding_failure{reason=\"check_conflicts_evaluation\",${BINDER_FILTER}}[${DURATION}s]))"

# Throughput delta + conflict rate snapshot (matches collect-metrics.sh layout)
SCHED_INCREASE=$(query_increase "sum(increase(scheduler_pod_scheduling_attempts{result=\"scheduled\",${SCHED_FILTER}}[${DURATION}s]))")
BIND_INCREASE=$(query_increase "sum(increase(binder_binding_pod_attempts{result=\"success\",${BINDER_FILTER}}[${DURATION}s]))")
# Primary conflict counter: ALL bind failures (any reason).
# Semantic match to para-scheduler's parasched_bind_result_total{result="conflict"}.
CONFLICT_INCREASE=$(query_increase "sum(increase(binder_binding_pod_attempts{result=\"failure\",${BINDER_FILTER}}[${DURATION}s]))")
# Strict counter: only resource-fit failures at bind time (kept as breakdown).
CONFLICT_STRICT_INCREASE=$(query_increase "sum(increase(binder_pod_binding_failure{reason=\"check_conflicts_evaluation\",${BINDER_FILTER}}[${DURATION}s]))")

cat > "$OUTPUT_DIR/throughput_delta.json" <<DELTA
{
  "duration_seconds": $DURATION,
  "scheduler_scheduled_delta": $(python3 -c "print(int(round(float('${SCHED_INCREASE}'))))" 2>/dev/null || echo 0),
  "scheduler_throughput_pods_per_sec": $(python3 -c "d=float('${SCHED_INCREASE}'); print(round(d/$DURATION, 2) if $DURATION>0 else 0)" 2>/dev/null || echo 0),
  "binder_success_delta": $(python3 -c "print(int(round(float('${BIND_INCREASE}'))))" 2>/dev/null || echo 0),
  "binder_throughput_pods_per_sec": $(python3 -c "d=float('${BIND_INCREASE}'); print(round(d/$DURATION, 2) if $DURATION>0 else 0)" 2>/dev/null || echo 0),
  "binder_conflict_delta": $(python3 -c "print(int(round(float('${CONFLICT_INCREASE}'))))" 2>/dev/null || echo 0),
  "binder_conflict_strict_delta": $(python3 -c "print(int(round(float('${CONFLICT_STRICT_INCREASE}'))))" 2>/dev/null || echo 0),
  "conflict_rate": $(python3 -c "
s=float('${BIND_INCREASE}')
c=float('${CONFLICT_INCREASE}')
print(round(c/(s+c), 4) if (s+c)>0 else 0)" 2>/dev/null || echo 0),
  "conflict_rate_strict": $(python3 -c "
s=float('${BIND_INCREASE}')
c=float('${CONFLICT_STRICT_INCREASE}')
print(round(c/(s+c), 4) if (s+c)>0 else 0)" 2>/dev/null || echo 0)
}
DELTA
echo "    scheduler: $(cat "$OUTPUT_DIR/throughput_delta.json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['scheduler_scheduled_delta']} pods / {d['duration_seconds']}s = {d['scheduler_throughput_pods_per_sec']} pods/s\")")"
echo "    binder:    $(cat "$OUTPUT_DIR/throughput_delta.json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['binder_success_delta']} success, {d['binder_conflict_delta']} conflicts, rate={d['conflict_rate']}\")")"

# ==============================================================
#  Snapshot diff: scrape-interval-independent counter/histogram deltas
# ==============================================================
# When the saturation phase is very short (< 30s), Prometheus 15s scrape may
# only capture 0-1 samples, making rate()/increase() return empty or zero.
# The snapshot diff approach directly queries raw counter values at START and
# SNAP_END, then computes the delta in Python.  This works as long as
# Prometheus has stored at least one sample before START and one after END.

echo ""
echo "=== Snapshot diff: scrape-interval-independent ==="

# Helper: instant query at a specific timestamp, return raw JSON
query_at() {
    local query="$1" ts="$2"
    curl -sf -G "$PROMETHEUS_URL/api/v1/query" \
        --data-urlencode "query=$query" \
        --data-urlencode "time=$ts" 2>/dev/null
}

# Helper: query a counter at START and SNAP_END, compute per-label delta.
snapshot_counter() {
    local name="$1" query="$2"
    echo "  [${name}] (snapshot diff)"
    local pre_f post_f
    pre_f=$(mktemp)
    post_f=$(mktemp)
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
snapshot_histogram() {
    local name="$1" metric_base="$2"
    echo "  [${name}] (snapshot histogram diff)"

    local metric_name="${metric_base%%\{*}"
    local selector=""
    if [[ "$metric_base" == *"{"* ]]; then
        selector="{${metric_base#*\{}"
    fi

    local tmpdir
    tmpdir=$(mktemp -d)
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
snapshot_counter "snap_scheduled" \
    "scheduler_pod_scheduling_attempts{result=\"scheduled\",${SCHED_FILTER}}"

snapshot_counter "snap_unschedulable" \
    "scheduler_pod_scheduling_attempts{result=\"unschedulable\",${SCHED_FILTER}}"

snapshot_counter "snap_error" \
    "scheduler_pod_scheduling_attempts{result=\"error\",${SCHED_FILTER}}"

snapshot_counter "snap_bind_success" \
    "binder_binding_pod_attempts{result=\"success\",${BINDER_FILTER}}"

snapshot_counter "snap_bind_failure" \
    "binder_binding_pod_attempts{result=\"failure\",${BINDER_FILTER}}"

snapshot_counter "snap_bind_conflict" \
    "binder_pod_binding_failure{reason=\"check_conflicts_evaluation\",${BINDER_FILTER}}"

# ---- Histograms ----
snapshot_histogram "snap_e2e_latency" \
    "scheduler_e2e_scheduling_duration_seconds{${SCHED_FILTER}}"

snapshot_histogram "snap_algo_latency" \
    "scheduler_scheduling_algorithm_duration_seconds{${SCHED_FILTER}}"

snapshot_histogram "snap_bind_latency" \
    "binder_e2e_duration_seconds{${BINDER_FILTER}}"

# ---- Derived summary ----
echo "  [snap_summary] (derived)"
python3 -c "
import json, os, sys

d = '$OUTPUT_DIR'
# duration_seconds = saturation window (no snap-end padding) — throughput divisor.
duration = $((END_TIME - START_TIME))

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
# bind_nf: all-reason bind failures — primary conflict counter.
# Semantic match to para-scheduler's parasched_bind_result_total{result=\"conflict\"}.
bind_nf = sum_delta(load('snap_bind_failure'))
# bind_cf: resource-fit conflicts only (check_conflicts_evaluation) — breakdown.
bind_cf = sum_delta(load('snap_bind_conflict'))

e2e     = load('snap_e2e_latency')
algo    = load('snap_algo_latency')
bind_l  = load('snap_bind_latency')

summary = {
    'duration_seconds': duration,
    'scheduling': {
        'scheduled': int(sched),
        'unschedulable': int(unsched),
        'error': int(err),
        'throughput_pods_per_sec': round(sched / duration, 2) if duration > 0 else 0,
    },
    'binding': {
        'success': int(bind_ok),
        'failure': int(bind_nf),
        # 'conflict' = all-reason failures; matches para's binding.conflict field.
        'conflict': int(bind_nf),
        # 'conflict_strict' = check_conflicts_evaluation only (breakdown).
        'conflict_strict': int(bind_cf),
        'conflict_rate': round(bind_nf / (bind_ok + bind_nf), 4) if (bind_ok + bind_nf) > 0 else 0,
        'conflict_rate_strict': round(bind_cf / (bind_ok + bind_cf), 4) if (bind_ok + bind_cf) > 0 else 0,
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
with open(os.path.join(d, 'snap_summary.json'), 'w') as f:
    json.dump(summary, f, indent=2)
    f.write('\n')
" 2>/dev/null || echo "    WARN: summary generation failed"

# Print key results from snapshot diff (most reliable for short windows)
if [ -f "$OUTPUT_DIR/snap_summary.json" ]; then
    python3 -c "
import json
with open('$OUTPUT_DIR/snap_summary.json') as f:
    s = json.load(f)
sch = s['scheduling']
bnd = s['binding']
lat = s['latency']
print(f'    [snap] scheduled={sch[\"scheduled\"]}, throughput={sch[\"throughput_pods_per_sec\"]} pods/s')
print(f'    [snap] bind_success={bnd[\"success\"]}, bind_failure={bnd[\"failure\"]}, conflict={bnd[\"conflict\"]}, conflict_rate={bnd[\"conflict_rate\"]}')
e2e_p99 = f'{lat[\"e2e_p99\"]*1000:.1f}ms' if lat.get('e2e_p99') else 'N/A'
print(f'    [snap] e2e_latency_p99={e2e_p99}')
" 2>/dev/null || true
fi

# ==============================================================
#  P1: Resource metrics (per-component CPU / memory)
# ==============================================================
echo ""
echo "=== P1: Resource metrics ==="

# Scheduler instances — namespace godel-system, pod prefix godel-scheduler-*
query_metric "scheduler_cpu" \
    'rate(container_cpu_usage_seconds_total{container="scheduler",namespace="godel-system",pod=~"godel-scheduler-.*"}[1m])'

query_metric "scheduler_memory_rss" \
    'container_memory_rss{container="scheduler",namespace="godel-system",pod=~"godel-scheduler-.*"}'

# Binder — single instance, pod prefix binder-*
query_metric "binder_cpu" \
    'rate(container_cpu_usage_seconds_total{container="binder",namespace="godel-system",pod=~"binder-.*"}[1m])'

query_metric "binder_memory_rss" \
    'container_memory_rss{container="binder",namespace="godel-system",pod=~"binder-.*"}'

# Dispatcher
query_metric "dispatcher_cpu" \
    'rate(container_cpu_usage_seconds_total{container="dispatcher",namespace="godel-system",pod=~"dispatcher-.*"}[1m])'

query_metric "dispatcher_memory_rss" \
    'container_memory_rss{container="dispatcher",namespace="godel-system",pod=~"dispatcher-.*"}'

echo ""
echo "Metrics collection complete: $OUTPUT_DIR/"
