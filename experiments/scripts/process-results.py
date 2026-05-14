#!/usr/bin/env python3
"""
Process raw experiment results into paper-ready data.

Data source priority (highest to lowest):
  1. cl2.log  — step timestamps, pod counts (ground truth from test harness)
  2. snap_*.json — Prometheus counter snapshot diffs (scrape-interval-independent)
  3. throughput_delta.json / total_*.json — Prometheus increase()/rate() (may be
     inaccurate for short experiments < 1 min due to 15s scrape interval)

Conflict definition:
  "conflict" in this script means all_candidates_failed (ACF), i.e. the Binder
  exhausted ALL K+1 candidates and returned the pod for full rescheduling.
  Individual candidate-level bind retries within the Binder are NOT counted as
  conflicts — they are resolved locally without rescheduling overhead.

Usage:
  python3 process-results.py experiments/results/          # process all
  python3 process-results.py experiments/results/A2-*      # process matching dirs
  python3 process-results.py experiments/results/ -o paper_data.csv

Output:
  - CSV file with one row per (experiment, trial), all paper-relevant metrics
  - JSON file with the same data for programmatic consumption
"""

import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Board D: benchmark output parsing (wrk, redis-benchmark, sysbench)
# ---------------------------------------------------------------------------

def parse_wrk(path: str) -> dict:
    """Parse wrk output for nginx benchmark.

    Returns dict with: qps, transfer_MBps, lat_avg_ms, lat_max_ms,
    lat_p50_ms, lat_p75_ms, lat_p90_ms, lat_p99_ms, total_requests.
    """
    result = {}
    if not os.path.isfile(path):
        return result
    with open(path, 'r', errors='replace') as f:
        text = f.read()

    # Requests/sec:  20041.15
    m = re.search(r'Requests/sec:\s+([\d.]+)', text)
    if m:
        result['qps'] = round(float(m.group(1)), 2)

    # Transfer/sec:     16.30MB
    m = re.search(r'Transfer/sec:\s+([\d.]+)([A-Za-z]+)', text)
    if m:
        val = float(m.group(1))
        unit = m.group(2).upper()
        if unit == 'GB':
            val *= 1024
        elif unit == 'KB':
            val /= 1024
        result['transfer_MBps'] = round(val, 2)

    # Total requests: "1202508 requests in 1.00m"
    m = re.search(r'(\d+)\s+requests\s+in', text)
    if m:
        result['total_requests'] = int(m.group(1))

    # Latency Distribution:  50% 3.72ms  75% 23.97ms  90% 50.76ms  99% 75.54ms
    for pct, key in [('50', 'lat_p50_ms'), ('75', 'lat_p75_ms'),
                     ('90', 'lat_p90_ms'), ('99', 'lat_p99_ms')]:
        m = re.search(rf'{pct}%\s+([\d.]+)([a-z]+)', text)
        if m:
            val, unit = float(m.group(1)), m.group(2)
            result[key] = round(_convert_time_to_ms(val, unit), 3)

    # Thread Stats header:  Avg  Stdev  Max  +/- Stdev
    # Latency    14.95ms   20.79ms 185.14ms   81.91%
    m = re.search(r'Latency\s+([\d.]+)([a-z]+)\s+([\d.]+)([a-z]+)\s+([\d.]+)([a-z]+)', text)
    if m:
        result['lat_avg_ms'] = round(_convert_time_to_ms(float(m.group(1)), m.group(2)), 3)
        result['lat_max_ms'] = round(_convert_time_to_ms(float(m.group(5)), m.group(6)), 3)

    return result


def _convert_time_to_ms(val: float, unit: str) -> float:
    """Convert wrk time unit to milliseconds."""
    unit = unit.lower()
    if unit == 'us':
        return val / 1000.0
    elif unit == 'ms':
        return val
    elif unit == 's':
        return val * 1000.0
    elif unit == 'm':
        return val * 60000.0
    return val


def parse_redis_benchmark(path: str) -> dict:
    """Parse redis-benchmark CSV output.

    Returns dict with: set_ops, get_ops (requests/sec).
    """
    result = {}
    if not os.path.isfile(path):
        return result
    with open(path, 'r', errors='replace') as f:
        for line in f:
            line = line.strip().strip('"')
            parts = [p.strip().strip('"') for p in line.split(',')]
            if len(parts) >= 2:
                try:
                    key = parts[0].strip('"').upper()
                    val = float(parts[1].strip('"'))
                    if key == 'SET':
                        result['set_ops'] = round(val, 2)
                    elif key == 'GET':
                        result['get_ops'] = round(val, 2)
                except ValueError:
                    pass
    return result


def parse_sysbench(path: str) -> dict:
    """Parse sysbench OLTP run output.

    Returns dict with: tps, qps, lat_avg_ms, lat_p95_ms, lat_min_ms, lat_max_ms,
    read_queries, write_queries, total_queries.
    """
    result = {}
    if not os.path.isfile(path):
        return result
    with open(path, 'r', errors='replace') as f:
        text = f.read()

    # transactions:  4663   (77.57 per sec.)
    m = re.search(r'transactions:\s+(\d+)\s+\(([\d.]+)\s+per sec', text)
    if m:
        result['transactions'] = int(m.group(1))
        result['tps'] = round(float(m.group(2)), 2)

    # queries:  93260  (1551.49 per sec.)
    m = re.search(r'queries:\s+(\d+)\s+\(([\d.]+)\s+per sec', text)
    if m:
        result['total_queries'] = int(m.group(1))
        result['qps'] = round(float(m.group(2)), 2)

    # read/write/other
    m = re.search(r'read:\s+(\d+)', text)
    if m:
        result['read_queries'] = int(m.group(1))
    m = re.search(r'write:\s+(\d+)', text)
    if m:
        result['write_queries'] = int(m.group(1))

    # Latency (ms):  min: 13.89  avg: 103.07  max: 449.12  95th percentile: 167.44
    m = re.search(r'min:\s+([\d.]+)', text)
    if m:
        result['lat_min_ms'] = round(float(m.group(1)), 3)
    m = re.search(r'avg:\s+([\d.]+)', text)
    if m:
        result['lat_avg_ms'] = round(float(m.group(1)), 3)
    m = re.search(r'max:\s+([\d.]+)', text)
    if m:
        result['lat_max_ms'] = round(float(m.group(1)), 3)
    m = re.search(r'95th percentile:\s+([\d.]+)', text)
    if m:
        result['lat_p95_ms'] = round(float(m.group(1)), 3)

    return result


def parse_pod_distribution(path: str) -> dict:
    """Parse kubectl get pods -o wide output to compute distribution balance.

    Returns dict with: pods_per_node (dict), total_pods, num_nodes,
    balance_stddev (standard deviation of pod counts across nodes).
    """
    result = {'pods_per_node': {}, 'total_pods': 0, 'num_nodes': 0, 'balance_stddev': 0.0}
    if not os.path.isfile(path):
        return result
    node_counts: dict[str, int] = {}
    with open(path, 'r', errors='replace') as f:
        for line in f:
            # Skip header
            if line.startswith('NAME') or not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 7:
                node = parts[6]  # NODE column
                node_counts[node] = node_counts.get(node, 0) + 1
    result['pods_per_node'] = node_counts
    result['total_pods'] = sum(node_counts.values())
    result['num_nodes'] = len(node_counts)
    if node_counts:
        vals = list(node_counts.values())
        mean = sum(vals) / len(vals)
        if len(vals) > 1:
            result['balance_stddev'] = round(
                math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)), 4)
    return result


# ---------------------------------------------------------------------------
# CL2 log parsing
# ---------------------------------------------------------------------------

# Step marker regex: I0413 14:19:26.710238 ... Step "[step: 03] Creating saturation pods" started
#
# We capture the fractional seconds suffix so saturation duration retains
# millisecond precision. Pre-fix the regex consumed `\.\d+` without capturing,
# which forced _parse_cl2_timestamp to round timestamps to whole seconds —
# producing scheduling_duration_s that was ±1s off the true wall-clock value
# and was unable to distinguish sub-second differences between pattern variants.
_STEP_RE = re.compile(
    r'^[A-Z](\d{4})\s+(\d{2}:\d{2}:\d{2}\.\d+)\s+\d+\s+\S+\]\s+'
    r'Step\s+"\[step:\s*\d+\]\s+(.+?)"\s+(started|ended)'
)

# Pod status line:
# WaitForControlledPodsRunning: namespace(...), controlledBy(...):
#   Pods: 100 out of 100 created, 0 running (0 updated), 0 pending scheduled,
#   100 not scheduled, 0 inactive, 0 terminating, 0 unknown, 0 runningButNotReady
_POD_STATUS_RE = re.compile(
    r'Pods:\s*(\d+)\s+out\s+of\s+(\d+)\s+created,\s*'
    r'(\d+)\s+running\s+\((\d+)\s+updated\),\s*'
    r'(\d+)\s+pending\s+scheduled,\s*'
    r'(\d+)\s+not\s+scheduled'
)

# Error line for timeout: "got context deadline exceeded while waiting for N pods"
_TIMEOUT_RE = re.compile(
    r'WaitForControlledPodsRunning:.*error.*context deadline exceeded.*'
    r'(\d+)\s+running.*?(\d+)\s+pending\s+scheduled.*?(\d+)\s+not\s+scheduled'
)

# SchedulingThroughput result embedded in CL2 log (JSON object in log line)
_SCHED_THROUGHPUT_RE = re.compile(r'SchedulingThroughput:\s*(\{.*\})')

# Test status
_TEST_STATUS_RE = re.compile(r'Status:\s*(Success|Fail)')


def _parse_cl2_timestamp(mmdd: str, hhmmss: str, year: int = None) -> float:
    """Parse CL2 log timestamp into a Unix timestamp with sub-second precision.

    Accepts either ``HH:MM:SS`` or ``HH:MM:SS.ffffff`` (fractional seconds).
    The CL2 / klog format always emits microsecond precision; the millisecond
    granularity matters because saturation throughput at 10000 nodes lands in
    the 1-3 second range, where rounding to whole seconds discards 30-100%
    of the signal.
    """
    if year is None:
        year = datetime.now().year
    month, day = int(mmdd[:2]), int(mmdd[2:])
    if '.' in hhmmss:
        hms, frac = hhmmss.split('.', 1)
        # Pad/truncate to 6 digits so it parses as microseconds regardless of
        # the source's precision (klog uses 6, but be defensive).
        frac = (frac + '000000')[:6]
        micro = int(frac)
    else:
        hms = hhmmss
        micro = 0
    h, m, s = hms.split(':')
    dt = datetime(year, month, day, int(h), int(m), int(s), micro, tzinfo=timezone.utc)
    return dt.timestamp()


def parse_cl2_log(log_path: str) -> dict:
    """Extract key information from a CL2 log file.

    Returns dict with:
      step_times: {step_name: {started: ts, ended: ts}}
      pod_summary: {created, running, pending_scheduled, not_scheduled}  (last status)
      timeout_namespaces: list of namespaces that timed out
      test_status: 'Success' | 'Fail' | 'Unknown'
      scheduling_throughput: parsed from CL2's SchedulingThroughput measurement
    """
    result = {
        'step_times': {},
        'timeout_namespaces': [],
        'timeout_not_scheduled': 0,     # from timeout error lines ONLY
        'timeout_pending_scheduled': 0,  # from timeout error lines ONLY
        'timeout_running': 0,            # from timeout error lines ONLY
        'test_status': 'Unknown',
        'scheduling_throughput': None,
    }

    if not os.path.isfile(log_path):
        return result

    year = datetime.now().year

    with open(log_path, 'r', errors='replace') as f:
        for line in f:
            # Step markers
            m = _STEP_RE.search(line)
            if m:
                mmdd, hhmmss, step_name, event = m.groups()
                ts = _parse_cl2_timestamp(mmdd, hhmmss, year)
                if step_name not in result['step_times']:
                    result['step_times'][step_name] = {}
                result['step_times'][step_name][event] = ts

            # Timeout errors — these are logged BEFORE the deletion phase starts,
            # so the pod counts here reflect the true scheduling state at timeout.
            # Example: "error for test-xxx/saturation-deployment-0: got context
            # deadline exceeded ... 0 running ..., 0 pending scheduled, 100 not scheduled"
            if 'context deadline exceeded' in line and 'WaitForControlledPodsRunning' in line:
                ns_m = re.search(r'namespace\(([^)]+)\)', line)
                if ns_m:
                    result['timeout_namespaces'].append(ns_m.group(1))
                # Extract pod counts from the timeout error line itself
                m_status = _POD_STATUS_RE.search(line)
                if m_status:
                    _, _, running, _, pending, not_sched = (
                        int(x) for x in m_status.groups())
                    result['timeout_not_scheduled'] += not_sched
                    result['timeout_pending_scheduled'] += pending
                    result['timeout_running'] += running

            # SchedulingThroughput JSON embedded in log
            m_thr = _SCHED_THROUGHPUT_RE.search(line)
            if m_thr:
                try:
                    result['scheduling_throughput'] = json.loads(m_thr.group(1))
                except json.JSONDecodeError:
                    pass

            # Test status
            m_st = _TEST_STATUS_RE.search(line)
            if m_st:
                result['test_status'] = m_st.group(1)

    return result


# ---------------------------------------------------------------------------
# Helpers: read metric files
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Optional[dict]:
    """Load a JSON file, return None on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _snap_sum_delta(snap_data: Optional[dict]) -> float:
    """Sum all deltas from a snapshot counter JSON."""
    if not snap_data or 'results' not in snap_data:
        return 0.0
    return sum(r.get('delta', 0) for r in snap_data['results'])


def _prom_instant_value(data: Optional[dict]) -> Optional[float]:
    """Extract the scalar value from a Prometheus query result.
    Handles both instant (vector) and range (matrix) result types.
    For matrix results, returns the last sample value.
    """
    if not data:
        return None
    try:
        results = data['data']['result']
        if results:
            r0 = results[0]
            # Instant query: "value": [timestamp, "val"]
            if 'value' in r0:
                v = float(r0['value'][1])
                return v if not math.isnan(v) else None
            # Range query: "values": [[ts, "val"], ...]
            if 'values' in r0 and r0['values']:
                v = float(r0['values'][-1][1])
                return v if not math.isnan(v) else None
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return None


def _prom_value_by_label(data: Optional[dict], label: str, value: str) -> Optional[float]:
    """Extract scalar value from a Prometheus query result, filtering by label.

    Use when a query returns multiple series and the caller needs a specific one
    (e.g. dispatcher_ready_at returns both binder=0 and dispatcher=<ts>; without
    filtering, results[0] picks binder by accident).
    """
    if not data:
        return None
    try:
        for r in data.get('data', {}).get('result', []):
            if r.get('metric', {}).get(label) != value:
                continue
            if 'value' in r:
                v = float(r['value'][1])
            elif r.get('values'):
                v = float(r['values'][-1][1])
            else:
                continue
            return v if not math.isnan(v) else None
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return None


def _prom_sum_last_values(data: Optional[dict]) -> Optional[int]:
    """Sum the last sample of every series in a Prometheus query result.

    Returns None when no series yielded a finite value, so callers can
    distinguish "metric never reported" from "metric reported zero".
    """
    if not data:
        return None
    try:
        agg = 0.0
        saw_any = False
        for r in data.get('data', {}).get('result', []):
            if 'value' in r:
                raw = r['value'][1]
            elif r.get('values'):
                raw = r['values'][-1][1]
            else:
                continue
            v = float(raw)
            if math.isnan(v):
                continue
            agg += v
            saw_any = True
        return int(round(agg)) if saw_any else None
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _prom_timeseries_values(data: Optional[dict]) -> list:
    """Extract (timestamp, value) pairs from a Prometheus range query result."""
    if not data:
        return []
    try:
        results = data['data']['result']
        if not results:
            return []
        values = []
        for r in results:
            for ts, val in r.get('values', []):
                try:
                    v = float(val)
                    if not math.isnan(v):
                        values.append((float(ts), v))
                except ValueError:
                    pass
        return values
    except (KeyError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Core: process a single trial
# ---------------------------------------------------------------------------

def process_trial(experiment_dir: str, trial_dir: str, config: dict) -> dict:
    """Process a single trial and return a flat dict of paper-relevant metrics.

    Data priority: cl2.log > snap_*.json > rate-based *.json
    """
    params = config.get('parameters', {})
    metrics_dir = os.path.join(trial_dir, 'metrics-saturation')

    # ---- 1. CL2 log (ground truth) ----
    cl2 = parse_cl2_log(os.path.join(trial_dir, 'cl2.log'))
    timing = _load_json(os.path.join(trial_dir, 'timing.json')) or {}

    # Compute expected total pods from config
    num_nodes = int(params.get('num_nodes', 0))
    ppn = int(params.get('pods_per_node', 29))
    expected_pods = num_nodes * ppn

    # Scheduling duration from CL2 step timestamps (most accurate)
    sat_start = None
    sat_end = None
    for step_name, times in cl2['step_times'].items():
        if 'Creating saturation pods' in step_name:
            sat_start = times.get('started')
        if 'Waiting for saturation pods to be running' in step_name:
            sat_end = times.get('ended')

    # Fallback to timing.json
    if sat_start is None:
        sat_start = (timing.get('saturation') or {}).get('start')
    if sat_end is None:
        sat_end = (timing.get('saturation') or {}).get('end')

    scheduling_duration = None
    if sat_start and sat_end:
        scheduling_duration = float(sat_end) - float(sat_start)

    is_timeout = len(cl2['timeout_namespaces']) > 0

    # Determine actually scheduled pods.
    #
    # KEY INSIGHT: CL2 logs pod status throughout the ENTIRE test lifecycle, including
    # the deletion phase (Step 06-07) where status lines show "0 out of 0 created,
    # 0 running".  Since the parser keeps the LAST status per namespace, these deletion-
    # phase lines overwrite the correct scheduling-phase counts.  This makes CL2 log
    # parsing unreliable for scheduled pod counts in non-timeout experiments.
    #
    # Strategy:
    #   - No timeout → CL2 WaitForControlledPodsRunning succeeded for all namespaces,
    #     meaning all expected pods reached Running state.  Use expected_pods directly.
    #   - Timeout → some namespaces timed out.  Parse the TIMEOUT ERROR lines (which
    #     report the pod status at the moment of timeout, before deletion starts) to
    #     count how many pods were NOT scheduled, and subtract from expected.
    if not is_timeout:
        # CL2 WaitForControlledPodsRunning succeeded for all namespaces →
        # all expected pods reached Running.  Don't parse status lines (they
        # get contaminated by the deletion phase "0 out of 0" reports).
        cl2_scheduled_pods = expected_pods
    else:
        # Timeout: use the pod counts from the timeout ERROR lines themselves.
        # These are logged at the moment of timeout, before deletion starts,
        # so they reflect the true scheduling state.
        # scheduled = expected - not_scheduled (pending_scheduled counts as scheduled
        # because the scheduler did assign a node, KWOK just hasn't confirmed yet).
        not_sched = cl2['timeout_not_scheduled']
        if not_sched > 0:
            cl2_scheduled_pods = expected_pods - not_sched
        else:
            cl2_scheduled_pods = None  # let Prometheus fallback handle it

    # ---- 2. Snapshot metrics (scrape-interval-independent) ----
    snap_summary = _load_json(os.path.join(metrics_dir, 'snap_summary.json'))
    snap_scheduled = _load_json(os.path.join(metrics_dir, 'snap_scheduled.json'))
    snap_unsched = _load_json(os.path.join(metrics_dir, 'snap_unschedulable.json'))
    snap_bind_ok = _load_json(os.path.join(metrics_dir, 'snap_bind_success.json'))
    snap_bind_cf = _load_json(os.path.join(metrics_dir, 'snap_bind_conflict.json'))
    snap_acf = _load_json(os.path.join(metrics_dir, 'snap_all_candidates_failed.json'))
    snap_e2e = _load_json(os.path.join(metrics_dir, 'snap_e2e_latency.json'))
    snap_algo = _load_json(os.path.join(metrics_dir, 'snap_algo_latency.json'))
    snap_bind_lat = _load_json(os.path.join(metrics_dir, 'snap_bind_latency.json'))

    # Counter deltas from snapshots
    prom_scheduled = _snap_sum_delta(snap_scheduled)
    prom_unschedulable = _snap_sum_delta(snap_unsched)
    prom_bind_success = _snap_sum_delta(snap_bind_ok)
    prom_bind_conflict = _snap_sum_delta(snap_bind_cf)
    prom_acf = _snap_sum_delta(snap_acf)

    # Filter out default-scheduler's contribution (label: profile=default-scheduler)
    # Only count para-scheduler profile
    parasched_scheduled = 0
    if snap_scheduled and 'results' in snap_scheduled:
        for r in snap_scheduled['results']:
            profile = r.get('metric', {}).get('profile', '')
            if profile != 'default-scheduler':
                parasched_scheduled += r.get('delta', 0)

    # ---- 3. Rate-based metrics (fallback) ----
    throughput_delta = _load_json(os.path.join(metrics_dir, 'throughput_delta.json'))

    # ---- Assemble final metrics (priority: cl2 > snap > rate) ----

    # -- Scheduled pods --
    # Best source: CL2 log (directly observed pod status)
    # Fallback: Prometheus snap_scheduled (may include default-scheduler)
    scheduled_pods = cl2_scheduled_pods
    scheduled_pods_source = 'cl2'
    if scheduled_pods is None or scheduled_pods == 0:
        scheduled_pods = int(round(parasched_scheduled))
        scheduled_pods_source = 'snap'
    if scheduled_pods == 0 and throughput_delta:
        scheduled_pods = int(throughput_delta.get('scheduler_scheduled_delta', 0))
        scheduled_pods_source = 'rate'

    # -- Throughput (pods/s) --
    # Compute from scheduled_pods / scheduling_duration (most accurate)
    throughput = None
    if scheduled_pods and scheduling_duration and scheduling_duration > 0:
        throughput = round(scheduled_pods / scheduling_duration, 2)

    # -- Conflict count (ACF = all_candidates_failed) --
    # This is the paper's definition: ACF triggers rescheduling, which is the real cost.
    # bind_conflict counts individual candidate retries within the Binder — not relevant.
    conflict_count = int(round(prom_acf))
    conflict_count_source = 'snap'

    # -- Conflict rate (ACF / (ACF + successfully_bound)) --
    # Use ACF-based rate, not bind_conflict-based rate.
    # For bind_success, prefer CL2 scheduled_pods over Prometheus counter because
    # short experiments (< 15s) may miss scrapes, making prom_bind_success inaccurate.
    effective_bind_success = scheduled_pods if (scheduled_pods and scheduled_pods > 0) else prom_bind_success
    total_binding_attempts = effective_bind_success + prom_acf
    conflict_rate = None
    if total_binding_attempts > 0:
        conflict_rate = round(prom_acf / total_binding_attempts, 4)

    # -- Latency (from snapshot histograms, most accurate) --
    def _get_lat(snap, field):
        if snap and field in snap:
            v = snap[field]
            return round(v * 1000, 3) if v is not None else None  # s → ms
        return None

    e2e_p50_ms = _get_lat(snap_e2e, 'p50')
    e2e_p99_ms = _get_lat(snap_e2e, 'p99')
    algo_p50_ms = _get_lat(snap_algo, 'p50')
    algo_p99_ms = _get_lat(snap_algo, 'p99')
    bind_p50_ms = _get_lat(snap_bind_lat, 'p50')
    bind_p99_ms = _get_lat(snap_bind_lat, 'p99')

    # -- Unschedulable count --
    unschedulable = int(round(prom_unschedulable))

    # -- Candidate-level bind conflict (for ablation analysis) --
    # This counts EVERY failed bind attempt at individual candidate level.
    # Useful for ablation: e.g. +MP has similar ACF to +M but far fewer bind
    # conflicts, showing penalty avoids conflicts at the candidate selection stage.
    bind_conflict_count = int(round(prom_bind_conflict))
    total_bind_attempts = prom_bind_success + prom_bind_conflict
    # For short experiments, use CL2 scheduled_pods as a more accurate denominator.
    # Each successfully scheduled pod produces exactly 1 bind_success, plus some
    # number of bind_conflicts from backup candidates that were tried first.
    effective_total_binds = (effective_bind_success + bind_conflict_count
                            if effective_bind_success > prom_bind_success
                            else total_bind_attempts)
    bind_conflict_rate = None
    if effective_total_binds > 0:
        bind_conflict_rate = round(bind_conflict_count / effective_total_binds, 4)

    # ---- 4. Microbenchmark C1: resource overhead (scheduler/binder/dispatcher) ----
    def _extract_resource_metric(data):
        """Extract resource metric from Prometheus matrix result.
        For multi-instance (scheduler), sum across all pods.
        Returns (sum_value, per_instance_values) or (None, []).
        """
        if not data:
            return None, []
        try:
            results = data.get('data', {}).get('result', [])
            vals = []
            for r in results:
                vs = r.get('values', [])
                if vs:
                    v = float(vs[-1][1])  # last sample
                    if not math.isnan(v):
                        vals.append(v)
            if vals:
                return round(sum(vals), 6), vals
        except (KeyError, ValueError, TypeError, IndexError):
            pass
        return None, []

    sched_cpu_total, sched_cpu_vals = _extract_resource_metric(
        _load_json(os.path.join(metrics_dir, 'scheduler_cpu.json')))
    sched_mem_total, sched_mem_vals = _extract_resource_metric(
        _load_json(os.path.join(metrics_dir, 'scheduler_memory_rss.json')))
    binder_cpu, _ = _extract_resource_metric(
        _load_json(os.path.join(metrics_dir, 'binder_cpu.json')))
    binder_mem, _ = _extract_resource_metric(
        _load_json(os.path.join(metrics_dir, 'binder_memory_rss.json')))
    dispatcher_cpu, _ = _extract_resource_metric(
        _load_json(os.path.join(metrics_dir, 'dispatcher_cpu.json')))
    dispatcher_mem, _ = _extract_resource_metric(
        _load_json(os.path.join(metrics_dir, 'dispatcher_memory_rss.json')))

    # ---- 5. Microbenchmark C2: per-pod scheduling overhead ----
    snap_cand_sel = _load_json(os.path.join(metrics_dir, 'snap_candidate_selection.json'))
    snap_dispatch = _load_json(os.path.join(metrics_dir, 'snap_dispatch_latency.json'))

    cand_sel_p50_ms = _get_lat(snap_cand_sel, 'p50')
    cand_sel_p99_ms = _get_lat(snap_cand_sel, 'p99')
    dispatch_lat_p50_ms = _get_lat(snap_dispatch, 'p50')
    dispatch_lat_p99_ms = _get_lat(snap_dispatch, 'p99')

    # Penalty lookup latency (from rate-based files as fallback)
    penalty_p50 = _prom_instant_value(_load_json(os.path.join(metrics_dir, 'penalty_lookup_p50.json')))
    penalty_p50_ms = round(penalty_p50 * 1000, 3) if penalty_p50 is not None else None

    # ---- 6. Microbenchmark C3: control plane overhead ----
    dispatch_throughput = _prom_instant_value(
        _load_json(os.path.join(metrics_dir, 'dispatch_throughput.json')))
    dispatch_queue = _load_json(os.path.join(metrics_dir, 'dispatch_queue_depth.json'))
    dispatch_queue_val = None
    if dispatch_queue:
        try:
            for r in dispatch_queue.get('data', {}).get('result', []):
                comp = r.get('metric', {}).get('component', '')
                if comp == 'dispatcher':
                    vs = r.get('values', [])
                    if vs:
                        dispatch_queue_val = float(vs[-1][1])
        except (KeyError, ValueError, TypeError):
            pass

    # Candidate rank distribution (which backup candidate was actually used)
    cand_rank_p50 = _prom_instant_value(_load_json(os.path.join(metrics_dir, 'candidate_rank_p50.json')))
    cand_rank_p99 = _prom_instant_value(_load_json(os.path.join(metrics_dir, 'candidate_rank_p99.json')))

    # ---- 7. Scheduling quality: node score ----
    node_score_p50 = _prom_instant_value(
        _load_json(os.path.join(metrics_dir, 'selected_node_score_p50.json')))

    # ---- 8. Sync metrics (ParSync): staleness & sync duration ----
    sync_dur_p50 = _prom_instant_value(_load_json(os.path.join(metrics_dir, 'sync_duration_p50.json')))
    sync_dur_p50_ms = round(sync_dur_p50 * 1000, 3) if sync_dur_p50 is not None else None
    sync_dur_p99 = _prom_instant_value(_load_json(os.path.join(metrics_dir, 'sync_duration_p99.json')))
    sync_dur_p99_ms = round(sync_dur_p99 * 1000, 3) if sync_dur_p99 is not None else None
    staleness_p50 = _prom_instant_value(_load_json(os.path.join(metrics_dir, 'partition_staleness_p50.json')))
    staleness_p50_ms = round(staleness_p50 * 1000, 3) if staleness_p50 is not None else None
    staleness_p99 = _prom_instant_value(_load_json(os.path.join(metrics_dir, 'partition_staleness_p99.json')))
    staleness_p99_ms = round(staleness_p99 * 1000, 3) if staleness_p99 is not None else None

    # ---- 9. Pod scheduling attempts ----
    pod_attempts_p50 = _prom_instant_value(
        _load_json(os.path.join(metrics_dir, 'pod_scheduling_attempts_p50.json')))
    pod_attempts_p99 = _prom_instant_value(
        _load_json(os.path.join(metrics_dir, 'pod_scheduling_attempts_p99.json')))

    # ---- 10. Cold-start observability (design-parsync-pull-fix.md §4.6.4) ----
    # Cold-start duration = max(first_snapshot_applied across partitions) - sat_start.
    # Sign convention (negative values are kept, not clipped):
    #   negative → all partitions completed first apply BEFORE saturation began
    #              (the readiness gate worked; ParSync was warm at load start).
    #   positive → cold-start window overlapped with the saturation phase
    #              (G5 in design §6.1 wants this < 3s).
    # NaN-tolerant: missing partitions are skipped rather than zeroing the result.
    fsa_data = _load_json(os.path.join(metrics_dir, 'first_snapshot_applied.json'))
    first_snap_max = None
    if fsa_data:
        try:
            for r in fsa_data.get('data', {}).get('result', []):
                v = r.get('value')
                if v and len(v) >= 2:
                    ts = float(v[1])
                    if not math.isnan(ts) and ts > 0:
                        first_snap_max = ts if first_snap_max is None else max(first_snap_max, ts)
        except (KeyError, ValueError, TypeError):
            pass
    cold_start_duration_s = None
    if first_snap_max is not None and sat_start is not None:
        cold_start_duration_s = round(first_snap_max - float(sat_start), 3)

    # Assumed pod count P99 (gauge sampled over experiment window). High values
    # indicate accumulating assumed pods — TTL bug or partitionID=-1 nodes
    # retaining state forever (see design §4.5.1).
    assumed_count_data = _load_json(os.path.join(metrics_dir, 'assumed_pod_count.json'))
    assumed_pod_count_p99 = None
    if assumed_count_data:
        all_samples: list = []
        try:
            for r in assumed_count_data.get('data', {}).get('result', []):
                for ts, val in r.get('values', []):
                    fv = float(val)
                    if not math.isnan(fv):
                        all_samples.append(fv)
        except (KeyError, ValueError, TypeError):
            pass
        if all_samples:
            all_samples.sort()
            p99_idx = max(0, int(0.99 * (len(all_samples) - 1)))
            assumed_pod_count_p99 = round(all_samples[p99_idx], 1)

    # Dispatcher ready timestamp — validates that the readinessProbe gate held
    # off Scheduler/Binder until the dispatcher finished partition labelling.
    # The query returns one series per scraped instance (binder, dispatcher,
    # sometimes scheduler); only the dispatcher series carries the real
    # readiness timestamp — the others are zero. Filter explicitly, otherwise
    # _prom_instant_value can pick the binder/zero series by accident.
    dispatcher_ready_at = _prom_value_by_label(
        _load_json(os.path.join(metrics_dir, 'dispatcher_ready_at.json')),
        'component', 'dispatcher')

    # Snapshot oversize total — reserved for the (currently fatal) path where a
    # single chunk exceeds the 1 MiB ConfigMap limit. Stays 0 unless that fatal
    # handling is relaxed.
    snapshot_oversize_total = _prom_instant_value(
        _load_json(os.path.join(metrics_dir, 'snapshot_oversize_total.json')))

    # Per-reason breakdown remains in the raw snapshot_publish_errors_total.json.
    snapshot_publish_errors_total = _prom_sum_last_values(
        _load_json(os.path.join(metrics_dir, 'snapshot_publish_errors_total.json')))

    snapshot_chunks_total = _prom_sum_last_values(
        _load_json(os.path.join(metrics_dir, 'snapshot_publish_chunks.json')))

    return {
        # Experiment identification
        'experiment': config.get('name', ''),
        'trial': (timing.get('trial', 1)),
        'timestamp': config.get('timestamp', ''),

        # Parameters (for grouping/filtering)
        'num_nodes': num_nodes,
        'num_schedulers': int(params.get('num_schedulers', 0)),
        'candidate_k': int(params.get('num_backup', 0)),
        'penalty_weight': float(params.get('conflict_penalty', 0)),
        'sync_period': float(params.get('sync_period', 0)),
        'num_partitions': int(params.get('num_partitions', 0)),
        'sync_pattern': params.get('sync_pattern', ''),
        'pods_per_node': ppn,

        # Core metrics (paper Table/Figure data)
        'expected_pods': expected_pods,
        'scheduled_pods': scheduled_pods,
        'scheduled_pods_source': scheduled_pods_source,
        # Sub-second precision preserved (CL2 log carries microseconds; rounded
        # to 3 decimals = ms, which matches the timing we actually observe).
        'scheduling_duration_s': round(scheduling_duration, 3) if scheduling_duration else None,
        'throughput_pods_per_s': throughput,
        'unschedulable_count': unschedulable,

        # Two-level conflict metrics:
        #   L1 (pod-level):  ACF = all_candidates_failed → triggers rescheduling
        #   L2 (candidate-level): bind_conflict → Binder retries within K+1 candidates
        # Paper's primary conflict metric is L1 (ACF). L2 is for ablation analysis.
        'acf_count': conflict_count,                  # L1: rescheduling events
        'acf_rate': conflict_rate,                    # L1: ACF / (ACF + scheduled_pods)
        'bind_conflict_count': bind_conflict_count,   # L2: candidate-level failures
        'bind_conflict_rate': bind_conflict_rate,     # L2: bind_conflict / total_bind_attempts

        # Latency (ms)
        'e2e_p50_ms': e2e_p50_ms,
        'e2e_p99_ms': e2e_p99_ms,
        'algo_p50_ms': algo_p50_ms,
        'algo_p99_ms': algo_p99_ms,
        'bind_p50_ms': bind_p50_ms,
        'bind_p99_ms': bind_p99_ms,

        # C1: Resource overhead (CPU in cores, memory in bytes)
        'scheduler_cpu_total': sched_cpu_total,
        'scheduler_mem_rss_total': round(sched_mem_total) if sched_mem_total is not None else None,
        'scheduler_instances': len(sched_cpu_vals) if sched_cpu_vals else None,
        'binder_cpu': binder_cpu,
        'binder_mem_rss': round(binder_mem) if binder_mem is not None else None,
        'dispatcher_cpu': dispatcher_cpu,
        'dispatcher_mem_rss': round(dispatcher_mem) if dispatcher_mem is not None else None,

        # C2: Per-pod scheduling overhead (ms)
        'cand_sel_p50_ms': cand_sel_p50_ms,
        'cand_sel_p99_ms': cand_sel_p99_ms,
        'penalty_lookup_p50_ms': penalty_p50_ms,

        # C3: Control plane overhead
        'dispatch_lat_p50_ms': dispatch_lat_p50_ms,
        'dispatch_lat_p99_ms': dispatch_lat_p99_ms,
        'dispatch_throughput': round(dispatch_throughput, 2) if dispatch_throughput is not None else None,
        'dispatch_queue_depth': dispatch_queue_val,
        'cand_rank_p50': cand_rank_p50,
        'cand_rank_p99': cand_rank_p99,

        # Scheduling quality
        'node_score_p50': node_score_p50,

        # Sync metrics (ParSync)
        'sync_duration_p50_ms': sync_dur_p50_ms,
        'sync_duration_p99_ms': sync_dur_p99_ms,
        'staleness_p50_ms': staleness_p50_ms,
        'staleness_p99_ms': staleness_p99_ms,

        # Pod scheduling attempts (how many tries before success)
        'pod_attempts_p50': pod_attempts_p50,
        'pod_attempts_p99': pod_attempts_p99,

        # Cold-start observability (design-parsync-pull-fix.md §4.6.4)
        'cold_start_duration_s': cold_start_duration_s,
        'assumed_pod_count_p99': assumed_pod_count_p99,
        'dispatcher_ready_at': int(dispatcher_ready_at) if dispatcher_ready_at is not None else None,
        'snapshot_oversize_total': int(snapshot_oversize_total) if snapshot_oversize_total is not None else None,
        'snapshot_publish_errors_total': snapshot_publish_errors_total,
        'snapshot_chunks_total': snapshot_chunks_total,

        # Diagnostic / reference
        'is_timeout': is_timeout,
        # CL2 "Fail" can mean SLO violation (latency/throughput threshold exceeded)
        # even when all pods were successfully scheduled.  Distinguish from real failure.
        'test_status': 'SLO_Fail' if (cl2['test_status'] == 'Fail' and not is_timeout) else cl2['test_status'],
        'bind_success': int(round(prom_bind_success)),
        'prom_scheduled': int(round(prom_scheduled)),
    }


# ---------------------------------------------------------------------------
# Board D: process a single workload benchmark trial
# ---------------------------------------------------------------------------

def process_workload_trial(experiment_dir: str, trial_dir: str, config: dict) -> dict:
    """Process a single Board D trial and return a flat dict of paper-relevant metrics.

    Board D data sources:
      - nginx-wrk.txt → QPS, latency P50/P99
      - redis-benchmark.csv → SET/GET OPS
      - sysbench-run.txt → TPS, QPS, P95 latency
      - cpu-util-*.json / mem-util-*.json → node resource utilization balance
      - pod-distribution.txt → pod placement balance
    """
    strategy = config.get('strategy', '')
    trial_num = int(os.path.basename(trial_dir).replace('trial-', ''))

    # ---- Benchmark results ----
    wrk = parse_wrk(os.path.join(trial_dir, 'nginx-wrk.txt'))
    redis = parse_redis_benchmark(os.path.join(trial_dir, 'redis-benchmark.csv'))
    sysbench = parse_sysbench(os.path.join(trial_dir, 'sysbench-run.txt'))

    # ---- Node resource utilization ----
    cpu_utils = []
    mem_utils = []
    for f in sorted(os.listdir(trial_dir)):
        fpath = os.path.join(trial_dir, f)
        if f.startswith('cpu-util-') and f.endswith('.json'):
            data = _load_json(fpath)
            v = _prom_instant_value(data)
            if v is not None:
                cpu_utils.append(v)
        elif f.startswith('mem-util-') and f.endswith('.json'):
            data = _load_json(fpath)
            v = _prom_instant_value(data)
            if v is not None:
                mem_utils.append(v)

    cpu_mean = round(sum(cpu_utils) / len(cpu_utils), 6) if cpu_utils else None
    cpu_stddev = round(_safe_stddev(cpu_utils), 6) if len(cpu_utils) > 1 else None
    mem_mean = round(sum(mem_utils) / len(mem_utils), 6) if mem_utils else None
    mem_stddev = round(_safe_stddev(mem_utils), 6) if len(mem_utils) > 1 else None

    # ---- Pod distribution balance ----
    pod_dist = parse_pod_distribution(os.path.join(experiment_dir, 'pod-distribution.txt'))

    return {
        # Experiment identification
        'experiment': config.get('experiment', ''),
        'trial': trial_num,
        'timestamp': config.get('timestamp', ''),
        'board': 'D',

        # Parameters
        'strategy': strategy,
        'num_schedulers': int(config.get('num_schedulers', 0)),
        'bench_duration': int(config.get('bench_duration', 60)),
        'replicas_per_workload': int(config.get('replicas_per_workload', 6)),

        # nginx (wrk) metrics
        'nginx_qps': wrk.get('qps'),
        'nginx_lat_avg_ms': wrk.get('lat_avg_ms'),
        'nginx_lat_p50_ms': wrk.get('lat_p50_ms'),
        'nginx_lat_p90_ms': wrk.get('lat_p90_ms'),
        'nginx_lat_p99_ms': wrk.get('lat_p99_ms'),
        'nginx_lat_max_ms': wrk.get('lat_max_ms'),
        'nginx_total_requests': wrk.get('total_requests'),

        # redis metrics
        'redis_set_ops': redis.get('set_ops'),
        'redis_get_ops': redis.get('get_ops'),

        # mysql (sysbench) metrics
        'mysql_tps': sysbench.get('tps'),
        'mysql_qps': sysbench.get('qps'),
        'mysql_lat_avg_ms': sysbench.get('lat_avg_ms'),
        'mysql_lat_p95_ms': sysbench.get('lat_p95_ms'),
        'mysql_lat_max_ms': sysbench.get('lat_max_ms'),
        'mysql_transactions': sysbench.get('transactions'),

        # Node resource utilization balance
        'cpu_util_mean': cpu_mean,
        'cpu_util_stddev': cpu_stddev,
        'mem_util_mean': mem_mean,
        'mem_util_stddev': mem_stddev,

        # Pod distribution balance
        'pod_balance_stddev': pod_dist['balance_stddev'],
        'pod_total': pod_dist['total_pods'],
        'pod_nodes': pod_dist['num_nodes'],
    }


def _is_workload_experiment(config: dict) -> bool:
    """Detect if a config.json belongs to Board D (workload benchmark)."""
    return 'strategy' in config and 'parameters' not in config


# ---------------------------------------------------------------------------
# Main: discover experiments and process
# ---------------------------------------------------------------------------

def discover_experiments(paths: list[str]) -> list[tuple[str, str]]:
    """Discover (experiment_dir, trial_dir) pairs from given paths.

    Handles:
      - Directory containing multiple experiment dirs (e.g. experiments/results/)
      - Glob patterns resolved by the shell (e.g. experiments/results/A2-*)
      - Single experiment dir (e.g. experiments/results/A2-2000n-P1_20260413/)
    """
    pairs = []
    for path in paths:
        path = path.rstrip('/')
        # Is this an experiment dir (has config.json)?
        if os.path.isfile(os.path.join(path, 'config.json')):
            for trial in sorted(Path(path).glob('trial-*')):
                if trial.is_dir():
                    pairs.append((path, str(trial)))
        # Is this a parent dir containing experiment dirs?
        elif os.path.isdir(path):
            for entry in sorted(os.listdir(path)):
                exp_dir = os.path.join(path, entry)
                if os.path.isfile(os.path.join(exp_dir, 'config.json')):
                    for trial in sorted(Path(exp_dir).glob('trial-*')):
                        if trial.is_dir():
                            pairs.append((exp_dir, str(trial)))
    return pairs


# ---------------------------------------------------------------------------
# Multi-trial aggregation (mean / stddev)
# ---------------------------------------------------------------------------

# Metric fields that should be aggregated across trials (Board A/B/C).
_AGGREGATE_FIELDS = [
    'scheduled_pods', 'scheduling_duration_s', 'throughput_pods_per_s',
    'acf_count', 'acf_rate', 'bind_conflict_count', 'bind_conflict_rate',
    'unschedulable_count',
    'e2e_p50_ms', 'e2e_p99_ms', 'algo_p50_ms', 'algo_p99_ms',
    'bind_p50_ms', 'bind_p99_ms',
    # C1: Resource overhead
    'scheduler_cpu_total', 'scheduler_mem_rss_total',
    'binder_cpu', 'binder_mem_rss', 'dispatcher_cpu', 'dispatcher_mem_rss',
    # C2: Per-pod overhead
    'cand_sel_p50_ms', 'cand_sel_p99_ms',
    'penalty_lookup_p50_ms',
    # C3: Control plane
    'dispatch_lat_p50_ms', 'dispatch_lat_p99_ms', 'dispatch_throughput',
    'cand_rank_p50', 'cand_rank_p99',
    # Scheduling quality
    'node_score_p50',
    # Sync
    'sync_duration_p50_ms', 'sync_duration_p99_ms',
    'staleness_p50_ms', 'staleness_p99_ms',
    # Pod attempts
    'pod_attempts_p50', 'pod_attempts_p99',
]

# Metric fields for Board D workload benchmark aggregation.
_WORKLOAD_AGGREGATE_FIELDS = [
    # nginx
    'nginx_qps', 'nginx_lat_avg_ms', 'nginx_lat_p50_ms',
    'nginx_lat_p90_ms', 'nginx_lat_p99_ms',
    # redis
    'redis_set_ops', 'redis_get_ops',
    # mysql
    'mysql_tps', 'mysql_qps', 'mysql_lat_avg_ms', 'mysql_lat_p95_ms',
    # resource balance
    'cpu_util_mean', 'cpu_util_stddev', 'mem_util_mean', 'mem_util_stddev',
    # pod balance
    'pod_balance_stddev',
]


def _safe_mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _safe_stddev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _safe_mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))


def _experiment_group_name(dirname: str) -> str:
    """Strip timestamp suffix: 'A2-2000n-P4_20260413_143240' -> 'A2-2000n-P4'."""
    m = re.match(r'^(.+?)_\d{8}_\d{6}$', dirname)
    return m.group(1) if m else dirname


def aggregate_rows(rows: list[dict]) -> list[dict]:
    """Group rows by experiment name and compute mean/stddev per metric.
    Handles both Board A/B/C (CL2) and Board D (workload) rows.
    """
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r['experiment']].append(r)

    agg_rows = []
    for name in sorted(groups):
        trials = groups[name]
        is_workload = trials[0].get('board') == 'D'

        if is_workload:
            agg = {
                'experiment': name,
                'board': 'D',
                'n_trials': len(trials),
                'strategy': trials[0].get('strategy', ''),
                'num_schedulers': trials[0].get('num_schedulers', 0),
                'bench_duration': trials[0].get('bench_duration', 60),
                'replicas_per_workload': trials[0].get('replicas_per_workload', 6),
            }
            fields = _WORKLOAD_AGGREGATE_FIELDS
        else:
            agg = {
                'experiment': name,
                'n_trials': len(trials),
                # Copy parameters from first trial (same for all trials).
                'num_nodes': trials[0]['num_nodes'],
                'num_schedulers': trials[0]['num_schedulers'],
                'candidate_k': trials[0]['candidate_k'],
                'penalty_weight': trials[0]['penalty_weight'],
                'sync_period': trials[0]['sync_period'],
                'num_partitions': trials[0]['num_partitions'],
                'sync_pattern': trials[0]['sync_pattern'],
                'pods_per_node': trials[0]['pods_per_node'],
                'expected_pods': trials[0]['expected_pods'],
            }
            fields = _AGGREGATE_FIELDS

        for field in fields:
            vals = [float(t[field]) for t in trials
                    if t.get(field) is not None and math.isfinite(float(t[field]))]
            agg[f'{field}_mean'] = round(_safe_mean(vals), 4) if vals else None
            agg[f'{field}_std'] = round(_safe_stddev(vals), 4) if vals else None
        agg_rows.append(agg)
    return agg_rows


# ---------------------------------------------------------------------------
# Figure data generation (aligned with design.md chart specs)
# ---------------------------------------------------------------------------

def generate_figures(agg_rows: list[dict], output_dir: str):
    """Generate JSON files for each paper figure. Each file contains
    {x_label, y_label, series: [{label, x, y, yerr}]} ready for plotting.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Index by experiment name for lookup.
    by_name: dict[str, dict] = {r['experiment']: r for r in agg_rows}

    def _series(names: list[str], x_key: str, y_field: str, label: str) -> dict:
        xs, ys, errs = [], [], []
        for n in names:
            r = by_name.get(n)
            if not r:
                continue
            x = r.get(x_key)
            y = r.get(f'{y_field}_mean')
            e = r.get(f'{y_field}_std', 0) or 0
            if x is not None and y is not None:
                xs.append(x)
                ys.append(y)
                errs.append(e)
        return {'label': label, 'x': xs, 'y': ys, 'yerr': errs}

    def _save(name: str, x_label: str, y_label: str, series_list: list[dict]):
        valid = [s for s in series_list if s['x']]
        if not valid:
            return
        path = os.path.join(output_dir, f'{name}.json')
        with open(path, 'w') as f:
            json.dump({'x_label': x_label, 'y_label': y_label, 'series': valid},
                      f, indent=2)

    # ---- Detect available scales/configs by scanning experiment names ----
    def _find(prefix: str, suffix: str) -> list[str]:
        """Find experiment names matching prefix*suffix, sorted by x-axis value."""
        matches = sorted(
            [n for n in by_name if n.startswith(prefix) and n.endswith(suffix)],
            key=lambda n: by_name[n].get('num_nodes', 0)
        )
        return matches

    def _find_sched(prefix: str, suffix: str) -> list[str]:
        matches = sorted(
            [n for n in by_name if n.startswith(prefix) and n.endswith(suffix)],
            key=lambda n: by_name[n].get('num_schedulers', 0)
        )
        return matches

    # ==== Board A1: Low-contention scale expansion ====
    for cfg in ['E1', 'E2', 'E3']:
        _find('A1-', f'-{cfg}')  # warm up
    for fig, metric, ylabel in [
        ('a', 'throughput_pods_per_s', 'Throughput (pods/s)'),
        ('b', 'scheduling_duration_s', 'Total scheduling time (s)'),
    ]:
        series = [_series(_find('A1-', f'-{c}'), 'num_nodes', metric, c)
                  for c in ['E1', 'E2', 'E3']]
        _save(f'A1-{fig}', 'Cluster size (nodes)', ylabel, series)

    # ==== Board A2: HC-1 scale expansion (event-driven) ====
    for fig, metric, ylabel in [
        ('a', 'throughput_pods_per_s', 'Throughput (pods/s)'),
        ('b', 'acf_rate', 'Conflict rate (ACF)'),
        ('c', 'algo_p99_ms', 'P99 scheduling latency (ms)'),
    ]:
        series_e = [_series(_find('A2-', f'-{c}'), 'num_nodes', metric, c)
                    for c in ['E1', 'E2', 'E3']]
        _save(f'A2-E-{fig}', 'Cluster size (nodes)', ylabel, series_e)

        series_p = [_series(_find('A2-', f'-{c}'), 'num_nodes', metric, c)
                    for c in ['P1', 'P2', 'P3', 'P4']]
        _save(f'A2-P-{fig}', 'Cluster size (nodes)', ylabel, series_p)

    # ==== Board A3: HC-1 scheduler scalability ====
    for fig, metric, ylabel in [
        ('a', 'throughput_pods_per_s', 'Throughput (pods/s)'),
        ('b', 'acf_rate', 'Conflict rate (ACF)'),
    ]:
        series_e = [_series(_find_sched('A3-', f'-{c}'), 'num_schedulers', metric, c)
                    for c in ['E2', 'E3']]
        _save(f'A3-E-{fig}', 'Number of schedulers', ylabel, series_e)

        series_p = [_series(_find_sched('A3-', f'-{c}'), 'num_schedulers', metric, c)
                    for c in ['P3', 'P4']]
        _save(f'A3-P-{fig}', 'Number of schedulers', ylabel, series_p)

    # ==== Board B: Ablation (bar charts) ====
    for suffix, configs in [
        ('E', ['AbE0-base', 'AbE1-M', 'AbE2-P', 'AbE3-MP']),
        ('P', ['AbP0-base', 'AbP1-M', 'AbP2-P', 'AbP3-MP', 'AbP4-ML', 'AbP5-MPL']),
    ]:
        for fig, metric, ylabel in [
            ('a', 'throughput_pods_per_s', 'Throughput (pods/s)'),
            ('b', 'acf_rate', 'Conflict rate (ACF)'),
            ('c', 'bind_conflict_rate', 'Bind conflict rate (candidate-level)'),
        ]:
            labels, ys, errs = [], [], []
            for cfg in configs:
                r = by_name.get(cfg)
                if not r:
                    continue
                y = r.get(f'{metric}_mean')
                e = r.get(f'{metric}_std', 0) or 0
                if y is not None:
                    labels.append(cfg)
                    ys.append(y)
                    errs.append(e)
            if labels:
                path = os.path.join(output_dir, f'B-{suffix}-{fig}.json')
                with open(path, 'w') as f:
                    json.dump({
                        'type': 'bar', 'x_label': 'Configuration',
                        'y_label': ylabel,
                        'labels': labels, 'y': ys, 'yerr': errs,
                    }, f, indent=2)

    # ==== Board A3-E-c: Speedup ratio vs E1 ====
    # Compute speedup = throughput(E2/E3) / throughput(E1) at same scheduler count
    # E1 reference comes from A1 data (single scheduler throughput)
    e1_thr = None
    for n in by_name:
        if n.startswith('A1-') and n.endswith('-E1'):
            r = by_name[n]
            t = r.get('throughput_pods_per_s_mean')
            if t is not None:
                e1_thr = t
                break
    if e1_thr and e1_thr > 0:
        for cfg in ['E2', 'E3']:
            names = _find_sched('A3-', f'-{cfg}')
            xs, ys, errs = [], [], []
            for n in names:
                r = by_name[n]
                t = r.get('throughput_pods_per_s_mean')
                e = r.get('throughput_pods_per_s_std', 0) or 0
                if t is not None:
                    xs.append(r.get('num_schedulers', 0))
                    ys.append(round(t / e1_thr, 3))
                    errs.append(round(e / e1_thr, 3))
            if xs:
                path = os.path.join(output_dir, f'A3-E-c-{cfg}.json')
                with open(path, 'w') as f:
                    json.dump({
                        'x_label': 'Number of schedulers',
                        'y_label': 'Speedup (vs E1)',
                        'series': [{'label': cfg, 'x': xs, 'y': ys, 'yerr': errs}],
                    }, f, indent=2)

    # ==== Board B: Ablation (bar charts) ====
    for suffix, configs in [
        ('E', ['AbE0-base', 'AbE1-M', 'AbE2-P', 'AbE3-MP']),
        ('P', ['AbP0-base', 'AbP1-M', 'AbP2-P', 'AbP3-MP', 'AbP4-ML', 'AbP5-MPL']),
    ]:
        for fig, metric, ylabel in [
            ('a', 'throughput_pods_per_s', 'Throughput (pods/s)'),
            ('b', 'acf_rate', 'Conflict rate (ACF)'),
            ('c', 'bind_conflict_rate', 'Bind conflict rate (candidate-level)'),
        ]:
            labels, ys, errs = [], [], []
            for cfg in configs:
                r = by_name.get(cfg)
                if not r:
                    continue
                y = r.get(f'{metric}_mean')
                e = r.get(f'{metric}_std', 0) or 0
                if y is not None:
                    labels.append(cfg)
                    ys.append(y)
                    errs.append(e)
            if labels:
                path = os.path.join(output_dir, f'B-{suffix}-{fig}.json')
                with open(path, 'w') as f:
                    json.dump({
                        'type': 'bar', 'x_label': 'Configuration',
                        'y_label': ylabel,
                        'labels': labels, 'y': ys, 'yerr': errs,
                    }, f, indent=2)

    # ==== Sensitivity: K, w, strategy ====
    for fig, metric, ylabel in [
        ('a', 'throughput_pods_per_s', 'Throughput (pods/s)'),
        ('b', 'acf_rate', 'Conflict rate (ACF)'),
    ]:
        for paradigm in ['E', 'P']:
            k_names = sorted(
                [n for n in by_name if n.startswith('Sens-K') and n.endswith(f'-{paradigm}')],
                key=lambda n: by_name[n].get('candidate_k', 0)
            )
            if k_names:
                s = _series(k_names, 'candidate_k', metric, paradigm)
                _save(f'Sens-K-{paradigm}-{fig}', 'Backup candidates (K)', ylabel, [s])

            w_names = sorted(
                [n for n in by_name if n.startswith('Sens-W') and n.endswith(f'-{paradigm}')],
                key=lambda n: by_name[n].get('penalty_weight', 0)
            )
            if w_names:
                s = _series(w_names, 'penalty_weight', metric, paradigm)
                _save(f'Sens-W-{paradigm}-{fig}', 'Penalty weight (w)', ylabel, [s])

    # ==== Board D: Workload benchmark (bar charts, D-a/b/c/d) ====
    d_strategies = ['D1-E2', 'D1-E3']

    def _d_bar(fig_name: str, metrics_labels: list[tuple], ylabel: str):
        """Generate a grouped bar chart for Board D figures."""
        chart_data = {'type': 'grouped_bar', 'x_label': 'Strategy',
                      'y_label': ylabel, 'groups': []}
        for metric, label in metrics_labels:
            group = {'label': label, 'strategies': [], 'y': [], 'yerr': []}
            for strat in d_strategies:
                r = by_name.get(strat)
                if not r:
                    continue
                y = r.get(f'{metric}_mean')
                e = r.get(f'{metric}_std', 0) or 0
                if y is not None:
                    group['strategies'].append(strat)
                    group['y'].append(y)
                    group['yerr'].append(e)
            if group['y']:
                chart_data['groups'].append(group)
        if chart_data['groups']:
            path = os.path.join(output_dir, f'{fig_name}.json')
            with open(path, 'w') as f:
                json.dump(chart_data, f, indent=2)

    # D-a: nginx QPS and P99 latency
    _d_bar('D-a', [
        ('nginx_qps', 'QPS'),
        ('nginx_lat_p99_ms', 'P99 Latency (ms)'),
    ], 'nginx performance')

    # D-b: redis OPS
    _d_bar('D-b', [
        ('redis_set_ops', 'SET OPS'),
        ('redis_get_ops', 'GET OPS'),
    ], 'redis performance (ops/s)')

    # D-c: mysql TPS and P95 latency
    _d_bar('D-c', [
        ('mysql_tps', 'TPS'),
        ('mysql_lat_p95_ms', 'P95 Latency (ms)'),
    ], 'mysql performance')

    # D-d: node resource utilization balance (stddev)
    _d_bar('D-d', [
        ('cpu_util_stddev', 'CPU util stddev'),
        ('mem_util_stddev', 'Memory util stddev'),
    ], 'Resource utilization balance')

    # ==== Board C1: Resource overhead (bar charts by config) ====
    for fig, metric, ylabel in [
        ('a', 'scheduler_cpu_total', 'Scheduler CPU (cores)'),
        ('b', 'scheduler_mem_rss_total', 'Scheduler Memory RSS (bytes)'),
    ]:
        # Compare across A2 HC-1 configs at a reference scale (10000n)
        labels, ys, errs = [], [], []
        for cfg in ['E1', 'E2', 'E3', 'P1', 'P2', 'P3', 'P4']:
            names = _find('A2-', f'-{cfg}')
            # Pick the largest scale available
            if names:
                r = by_name.get(names[-1])
                if r:
                    y = r.get(f'{metric}_mean')
                    e = r.get(f'{metric}_std', 0) or 0
                    if y is not None:
                        labels.append(cfg)
                        ys.append(y)
                        errs.append(e)
        if labels:
            path = os.path.join(output_dir, f'C1-{fig}.json')
            with open(path, 'w') as f:
                json.dump({
                    'type': 'bar', 'x_label': 'Configuration',
                    'y_label': ylabel,
                    'labels': labels, 'y': ys, 'yerr': errs,
                }, f, indent=2)

    n_files = len([f for f in os.listdir(output_dir) if f.endswith('.json')]) if os.path.isdir(output_dir) else 0
    print(f'Figure data: {output_dir}/ ({n_files} files)')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Process experiment results into paper-ready data.',
        epilog='Examples:\n'
               '  %(prog)s experiments/results/              # per-trial CSV + JSON\n'
               '  %(prog)s experiments/results/ --aggregate   # + aggregated summary + figures\n'
               '  %(prog)s experiments/results/A2-*           # process matching dirs only\n',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='+',
                        help='Result directories or parent directory')
    parser.add_argument('-o', '--output', default='paper_data',
                        help='Output file basename (default: paper_data)')
    parser.add_argument('--aggregate', action='store_true',
                        help='Also produce aggregated summary (mean/std) and figure data')
    args = parser.parse_args()

    pairs = discover_experiments(args.paths)
    if not pairs:
        print('No experiment results found.', file=sys.stderr)
        sys.exit(1)

    print(f'Found {len(pairs)} trial(s) across {len(set(p[0] for p in pairs))} experiment(s)')

    sched_rows = []   # Board A/B/C (CL2 + Prometheus)
    workload_rows = []  # Board D (benchmark)
    for exp_dir, trial_dir in pairs:
        config = _load_json(os.path.join(exp_dir, 'config.json'))
        if not config:
            print(f'  SKIP {exp_dir} (no config.json)', file=sys.stderr)
            continue

        trial_name = os.path.basename(trial_dir)
        exp_name = config.get('experiment', config.get('name', os.path.basename(exp_dir)))
        print(f'  Processing {exp_name}/{trial_name}...', end='')

        try:
            if _is_workload_experiment(config):
                row = process_workload_trial(exp_dir, trial_dir, config)
                workload_rows.append(row)
                nginx_qps = row.get('nginx_qps') or 0
                mysql_tps = row.get('mysql_tps') or 0
                redis_set = row.get('redis_set_ops') or 0
                print(f' D | nginx={nginx_qps} qps | mysql={mysql_tps} tps '
                      f'| redis_set={redis_set} ops')
            else:
                row = process_trial(exp_dir, trial_dir, config)
                sched_rows.append(row)
                status = 'TIMEOUT' if row['is_timeout'] else 'OK'
                thr = row['throughput_pods_per_s'] or 0
                acf = row['acf_count']
                print(f' {status} | {row["scheduled_pods"]}/{row["expected_pods"]} pods '
                      f'| {thr} pods/s | ACF={acf}')
        except Exception as e:
            print(f' ERROR: {e}', file=sys.stderr)
            import traceback
            traceback.print_exc()

    rows = sched_rows + workload_rows
    if not rows:
        print('No data processed.', file=sys.stderr)
        sys.exit(1)

    # ---- Per-trial CSV (separate files for sched vs workload due to different schemas) ----
    if sched_rows:
        csv_path = args.output + '.csv'
        fieldnames = list(sched_rows[0].keys())
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sched_rows)
        print(f'\nPer-trial CSV (A/B/C): {csv_path} ({len(sched_rows)} rows)')

    if workload_rows:
        csv_path = args.output + '_workload.csv'
        fieldnames = list(workload_rows[0].keys())
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(workload_rows)
        print(f'Per-trial CSV (D): {csv_path} ({len(workload_rows)} rows)')

    # ---- Per-trial JSON ----
    json_path = args.output + '.json'
    with open(json_path, 'w') as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    print(f'Per-trial JSON: {json_path} ({len(rows)} rows)')

    # ---- Aggregated outputs (if requested) ----
    if args.aggregate:
        agg_rows = aggregate_rows(rows)

        agg_csv = args.output + '_agg.csv'
        # Separate aggregated CSVs for different schemas
        agg_sched = [r for r in agg_rows if r.get('board') != 'D']
        agg_work = [r for r in agg_rows if r.get('board') == 'D']
        if agg_sched:
            agg_fields = list(agg_sched[0].keys())
            with open(agg_csv, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=agg_fields)
                writer.writeheader()
                writer.writerows(agg_sched)
            print(f'Aggregated CSV (A/B/C): {agg_csv} ({len(agg_sched)} groups)')
        if agg_work:
            agg_work_csv = args.output + '_workload_agg.csv'
            agg_fields = list(agg_work[0].keys())
            with open(agg_work_csv, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=agg_fields)
                writer.writeheader()
                writer.writerows(agg_work)
            print(f'Aggregated CSV (D): {agg_work_csv} ({len(agg_work)} groups)')

        agg_json = args.output + '_agg.json'
        with open(agg_json, 'w') as f:
            json.dump(agg_rows, f, indent=2, ensure_ascii=False)
        print(f'Aggregated JSON: {agg_json} ({len(agg_rows)} groups)')

        fig_dir = os.path.dirname(args.output) or '.'
        fig_dir = os.path.join(fig_dir, 'figures')
        generate_figures(agg_rows, fig_dir)

    # ---- Summary tables ----
    if sched_rows:
        print(f'\n{"="*130}')
        print(f'  Board A/B/C: Scheduling Experiments')
        print(f'{"="*130}')
        print(f'{"Experiment":<25} {"Pods":>8} {"Thr(p/s)":>10} '
              f'{"ACF":>5} {"ACF%":>7} {"BConf":>6} {"BC%":>7} '
              f'{"algo_p99":>10} {"Duration":>10} {"Status":>8}')
        print(f'{"-"*130}')
        for r in sched_rows:
            acf_r = f'{r["acf_rate"]*100:.2f}%' if r['acf_rate'] is not None else 'N/A'
            bc_r = f'{r["bind_conflict_rate"]*100:.2f}%' if r['bind_conflict_rate'] is not None else 'N/A'
            algo = f'{r["algo_p99_ms"]:.1f}ms' if r['algo_p99_ms'] is not None else 'N/A'
            dur = f'{r["scheduling_duration_s"]:.1f}s' if r['scheduling_duration_s'] else 'N/A'
            thr = f'{r["throughput_pods_per_s"]:.1f}' if r['throughput_pods_per_s'] else 'N/A'
            status = 'TIMEOUT' if r['is_timeout'] else r['test_status']
            trial_suffix = f'/t{r["trial"]}' if r['trial'] > 1 else ''
            print(f'{r["experiment"] + trial_suffix:<25} '
                  f'{r["scheduled_pods"]:>8} {thr:>10} '
                  f'{r["acf_count"]:>5} {acf_r:>7} '
                  f'{r["bind_conflict_count"]:>6} {bc_r:>7} '
                  f'{algo:>10} {dur:>10} {status:>8}')
        print(f'{"="*130}')
        print(f'\nTwo-level conflict metrics:')
        print(f'  ACF  (L1, pod-level)       = all_candidates_failed -> triggers rescheduling')
        print(f'  BConf (L2, candidate-level) = individual bind failures -> resolved by Binder retry')
        print(f'  ACF% = ACF / (ACF + scheduled_pods);  BC% = BConf / (BConf + bind_success)')
        print(f'  Ablation insight: similar ACF but lower BConf => penalty avoids conflicts at selection stage')

    if workload_rows:
        print(f'\n{"="*130}')
        print(f'  Board D: Workload Benchmark')
        print(f'{"="*130}')
        print(f'{"Experiment":<15} {"T#":>3} '
              f'{"nginx QPS":>10} {"ng P99ms":>9} '
              f'{"redis SET":>10} {"redis GET":>10} '
              f'{"mysql TPS":>10} {"my P95ms":>9} '
              f'{"CPU std":>8} {"MEM std":>8}')
        print(f'{"-"*130}')
        for r in workload_rows:
            def _f(v, fmt='.1f'):
                return f'{v:{fmt}}' if v is not None else 'N/A'
            print(f'{r["experiment"]:<15} {r["trial"]:>3} '
                  f'{_f(r["nginx_qps"]):>10} {_f(r["nginx_lat_p99_ms"], ".2f"):>9} '
                  f'{_f(r["redis_set_ops"]):>10} {_f(r["redis_get_ops"]):>10} '
                  f'{_f(r["mysql_tps"]):>10} {_f(r["mysql_lat_p95_ms"], ".2f"):>9} '
                  f'{_f(r["cpu_util_stddev"], ".4f"):>8} {_f(r["mem_util_stddev"], ".4f"):>8}')
        print(f'{"="*130}')


if __name__ == '__main__':
    main()
