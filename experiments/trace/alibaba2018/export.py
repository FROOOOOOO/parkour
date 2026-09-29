#!/usr/bin/env python3
"""Turn the Alibaba cluster trace into the arrival-rate figure's input.

Board G is offline: instead of a cluster run it streams the published trace once
and reduces it to a per-second arrival series, then to the numbers the paper
quotes. This script owns that reduction; `figures/plot-trace-arrival-rate.py`
only draws the result.

The trace itself is not redistributed. Download `batch_task.csv` from the
Alibaba cluster-trace-v2018 release and point `--data-dir` at the directory
holding it:

    https://github.com/alibaba/clusterdata/tree/master/cluster-trace-v2018

The first run streams the 765 MiB table once and caches the per-second series as
`arrival-rate-series.npz`; later runs reuse that cache unless `--rebuild-cache`
is passed.

Arrival semantics: a task row contributes `instance_num` pods at its
`start_time`, which is what a scheduler would see. Rows whose fields do not
parse, or which fall outside the trace's own reporting window, are skipped.

Usage:
    python export.py --data-dir <trace-dir>
    python export.py --data-dir <trace-dir> --full-span
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import sys
import time
from collections import Counter

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import data as envelope  # noqa: E402

FIGURE = "trace-arrival-rate"
GENERATOR = "trace/alibaba2018/export.py"

DEFAULT_DATA_DIR = os.path.join(_EXPERIMENTS, "..", "bin", "clusterdata",
                                "cluster-trace-v2018", "data")
DEFAULT_CACHE_DIR = os.path.join(_HERE, "figures")
CACHE_NAME = "arrival-rate-series.npz"

# The representative burst window the right-hand panel draws, in trace seconds.
# These bounds come from the reference scan of the same trace and are kept here
# so this script needs no companion module.
WINDOW_START = 627120
WINDOW_END = 627179

DEFAULT_CAP = 200.0        # approximate Godel single-scheduler ceiling, pods/s
CDF_WINDOWS = (1, 60)      # window sizes drawn in the left panel
STAT_WINDOWS = (1, 10, 60, 300)  # window sizes reported in the stats block

# Active-period rule (see active_span()). The threshold is a fraction of the
# trace mean arrival rate rather than the 200 pods/s reference, so the reported
# "share of windows above 200 pods/s" is not defined in terms of the cap itself.
# The boundary is insensitive to both knobs: any threshold in 10%-20% of the
# mean (184-368 pods/s) and any duration in 0.5-2 h select the same period to
# within 0.5 h, because the excluded stretches are ~20 h long and ~99.8% empty.
ACTIVE_MIN_RATE_FRAC = 0.10   # of the trace mean rate  (== 184 pods/s here)
ACTIVE_MIN_DURATION = 3600.0  # seconds a low stretch must last to be trimmed
ACTIVE_WINDOW = 60            # window size the rule is evaluated on


def num(value):
    """Parse a trace cell to float, returning None for empty / NaN / invalid."""

    try:
        parsed = float(value)
        return None if math.isnan(parsed) else parsed
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  Trace scan
# ---------------------------------------------------------------------------

def scan_batch_task(path, progress_every=2_000_000):
    """Stream batch_task.csv once and count instance-expanded arrivals per second.

    Args:
        path:           Path to the uncompressed batch_task.csv (765 MiB).
        progress_every: Print a progress line every N rows; 0 disables it.

    Returns:
        dict with
          arrivals_pod:  Counter second -> pods (instance_num expanded)
          arrivals_task: Counter second -> accepted task rows
          total_pods:    int, sum of the instance counts of accepted rows
          total_tasks:   int, number of accepted task rows
          dropped:       int, rows rejected by the validity filter
          rows:          int, physical rows read

    Raises:
        FileNotFoundError: If `path` does not exist.
    """
    arrivals_pod = Counter()
    arrivals_task = Counter()
    total_pods = 0
    total_tasks = 0
    dropped = 0
    rows = 0
    t_begin = time.time()

    with open(path, newline="") as f:
        for row in csv.reader(f):
            rows += 1
            if progress_every and rows % progress_every == 0:
                print(f"    ... {rows:,} rows ({time.time() - t_begin:.0f}s)",
                      flush=True)
            if len(row) < 9:
                dropped += 1
                continue
            ts = num(row[5])
            inst = num(row[1])
            if ts is None or ts <= 0 or inst is None or inst <= 0:
                dropped += 1
                continue
            n = int(inst)
            sec = int(ts)
            arrivals_pod[sec] += n
            arrivals_task[sec] += 1
            total_pods += n
            total_tasks += 1

    return {
        "arrivals_pod": arrivals_pod,
        "arrivals_task": arrivals_task,
        "total_pods": total_pods,
        "total_tasks": total_tasks,
        "dropped": dropped,
        "rows": rows,
    }


def bin_counts(per_sec, window):
    """Aggregate a per-second count array into fixed windows aligned at index 0.

    Args:
        per_sec: 1-D integer array of pods per second covering the trace span.
        window:  Window size in seconds (>= 1).

    Returns:
        1-D int64 array of pods per window; the last window may be partial.
    """
    if window == 1:
        return per_sec.astype(np.int64, copy=False)
    span = per_sec.size
    nbins = int(math.ceil(span / window))
    padded = np.zeros(nbins * window, dtype=np.int64)
    padded[:span] = per_sec
    return padded.reshape(nbins, window).sum(axis=1)


def active_span(pods_per_s, min_rate=None, min_rate_frac=ACTIVE_MIN_RATE_FRAC,
                min_duration=ACTIVE_MIN_DURATION, window=ACTIVE_WINDOW):
    """Locate the trace's active period by trimming its quiet head and tail.

    Rule (one sentence, as reported in the paper): evaluate the mean arrival rate
    over consecutive `window`-second bins and drop the leading and the trailing
    maximal run of bins whose rate stays below `min_rate`, but only when that run
    lasts at least `min_duration`. Nothing between the two ends is touched, so
    empty windows inside the active period remain part of every statistic.

    The default threshold is a fraction of the trace mean rate (not the
    200 pods/s single-scheduler reference), which keeps the reported above-cap
    shares independent of the trimming criterion.

    Args:
        pods_per_s:    Per-second pod arrivals over the whole trace span.
        min_rate:      Absolute threshold in pods/s; overrides `min_rate_frac`.
        min_rate_frac: Threshold as a fraction of the trace mean rate.
        min_duration:  Minimum length in seconds of a trimmed head/tail run.
        window:        Bin size in seconds the rule is evaluated on.

    Returns:
        (i0, i1, info): half-open index range into `pods_per_s` plus a dict
        describing the rule and what it removed.
    """
    rates = bin_counts(pods_per_s, window) / float(window)
    mean_rate = float(pods_per_s.sum()) / pods_per_s.size
    threshold = float(min_rate) if min_rate is not None \
        else mean_rate * float(min_rate_frac)

    low = rates < threshold

    def leading_run(mask):
        """Length of the leading run of True values."""
        hit = np.flatnonzero(~mask)
        return int(hit[0]) if hit.size else int(mask.size)

    head_bins = leading_run(low)
    tail_bins = leading_run(low[::-1])
    if head_bins * window < min_duration:
        head_bins = 0
    if tail_bins * window < min_duration:
        tail_bins = 0
    # A degenerate rule must not empty the population.
    if head_bins + tail_bins >= rates.size:
        head_bins = tail_bins = 0

    i0 = head_bins * window
    i1 = min((rates.size - tail_bins) * window, pods_per_s.size)
    total = int(pods_per_s.sum())
    kept = int(pods_per_s[i0:i1].sum())
    info = {
        "rule_window_s": window,
        "threshold_pods_per_s": threshold,
        "threshold_frac_of_mean": (None if min_rate is not None
                                   else float(min_rate_frac)),
        "min_duration_s": float(min_duration),
        "head_trimmed_s": int(i0),
        "tail_trimmed_s": int(pods_per_s.size - i1),
        "active_span_s": int(i1 - i0),
        "pods_excluded": total - kept,
        "frac_pods_excluded": (total - kept) / total if total else 0.0,
    }
    return i0, i1, info


def build_cache(data_dir, cache_path, progress_every=2_000_000):
    """Scan batch_task.csv and persist the dense arrival series to `cache_path`.

    Args:
        data_dir:       Directory holding batch_task.csv.
        cache_path:     Destination .npz path.
        progress_every: Row interval for progress output.

    Returns:
        The cache dict as returned by load_cache().
    """
    csv_path = os.path.join(data_dir, "batch_task.csv")
    print(f"[scan] {csv_path}", flush=True)
    res = scan_batch_task(csv_path, progress_every=progress_every)

    secs = np.fromiter(res["arrivals_pod"].keys(), dtype=np.int64,
                       count=len(res["arrivals_pod"]))
    t_start = int(secs.min())
    t_end = int(secs.max())
    span = t_end - t_start + 1

    pods_per_s = np.zeros(span, dtype=np.int64)
    tasks_per_s = np.zeros(span, dtype=np.int64)
    for sec, pods in res["arrivals_pod"].items():
        pods_per_s[sec - t_start] = pods
    for sec, tasks in res["arrivals_task"].items():
        tasks_per_s[sec - t_start] = tasks

    meta = {
        "source_csv": csv_path,
        "source_bytes": os.path.getsize(csv_path),
        "csv_rows": res["rows"],
        "total_tasks": res["total_tasks"],
        "total_pods": res["total_pods"],
        "dropped_rows": res["dropped"],
        "t_start": t_start,
        "t_end": t_end,
        "span_s": span,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "semantics": ("arrival second = floor(start_time), pods = instance_num, "
                      "rows with unparsable or out-of-window fields are skipped"),
    }

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    # int32 halves the cache size; the busiest second of the trace holds ~2.7e5
    # pods, so the per-second and per-60 s counts stay far below 2^31.
    np.savez_compressed(
        cache_path,
        pods_per_s=pods_per_s.astype(np.int32),
        tasks_per_s=tasks_per_s.astype(np.int32),
        pods_per_60s=bin_counts(pods_per_s, 60).astype(np.int32),
        meta=json.dumps(meta),
    )
    print(f"[scan] cached {span:,} seconds -> {cache_path} "
          f"({os.path.getsize(cache_path) / 1e6:.1f} MB)", flush=True)
    return load_cache(cache_path)


def load_cache(cache_path):
    """Load the arrival series cache written by build_cache().

    Args:
        cache_path: Path to the .npz cache.

    Returns:
        dict with pods_per_s, tasks_per_s, pods_per_60s arrays (widened to int64
        so downstream sums cannot overflow) and the meta dict.

    Raises:
        FileNotFoundError: If the cache is missing.
    """
    with np.load(cache_path, allow_pickle=False) as z:
        out = {
            "pods_per_s": z["pods_per_s"].astype(np.int64),
            "tasks_per_s": z["tasks_per_s"].astype(np.int64),
            "pods_per_60s": z["pods_per_60s"].astype(np.int64),
            "meta": json.loads(str(z["meta"])),
        }
    return out


def export_series_csv(cache, output_dir, windows=(1, 60)):
    """Write the per-window rate series as gzip CSV for non-numpy consumers.

    Args:
        cache:      Cache dict from load_cache()/build_cache().
        output_dir: Destination directory.
        windows:    Window sizes to export.

    Returns:
        List of written paths.
    """
    written = []
    t_start = cache["meta"]["t_start"]
    for w in windows:
        counts = bin_counts(cache["pods_per_s"], w)
        path = os.path.join(output_dir, f"arrival-rate-{w}s.csv.gz")
        with gzip.open(path, "wt", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["window_start_s", "pods", "pods_per_s"])
            for i, c in enumerate(counts):
                wr.writerow([t_start + i * w, int(c), f"{c / w:.6f}"])
        written.append(path)
    return written


# ---------------------------------------------------------------------------
#  Statistics
# ---------------------------------------------------------------------------

def window_stats(pods_per_s, window, cap):
    """Rate quantiles and above-cap shares for one fixed window size.

    Args:
        pods_per_s: Per-second pod arrivals over the whole trace span.
        window:     Window size in seconds.
        cap:        Reference rate in pods/s (a window "exceeds" it when strictly
                    greater).

    Returns:
        dict of scalar statistics; `*_loaded` entries restrict the population to
        windows with at least one pod arrival. Percentiles are reported twice:
        plain `pNN` uses numpy's default linear interpolation, `pNN_higher` uses
        the inverted-CDF ("higher") quantile convention, which is the one the
        paper's trace statistics were first recorded with.
    """
    counts = bin_counts(pods_per_s, window)
    rates = counts / float(window)
    loaded = rates > 0
    rates_loaded = rates[loaded]
    qs = (50, 90, 95, 99)

    def pct(a, q, method):
        return float(np.percentile(a, q, method=method)) if a.size else float("nan")

    return {
        "window_s": window,
        "n_windows": int(rates.size),
        "n_windows_loaded": int(loaded.sum()),
        "frac_loaded": float(loaded.mean()),
        "mean": float(rates.mean()),
        "mean_loaded": float(rates_loaded.mean()) if rates_loaded.size else 0.0,
        "max": float(rates.max()),
        **{f"p{q}": pct(rates, q, "linear") for q in qs},
        **{f"p{q}_higher": pct(rates, q, "higher") for q in qs},
        **{f"p{q}_loaded": pct(rates_loaded, q, "linear") for q in qs},
        "frac_above_cap_all": float((rates > cap).mean()),
        "frac_above_cap_loaded": (float((rates_loaded > cap).mean())
                                  if rates_loaded.size else float("nan")),
        "n_above_cap": int((rates > cap).sum()),
    }


def representative_window_stats(cache, window_start, window_end, cap):
    """Per-second series and summary of the representative window.

    Args:
        cache:        Cache dict.
        window_start: First trace second of the window (inclusive).
        window_end:   Last trace second of the window (inclusive).
        cap:          Reference rate in pods/s.

    Returns:
        (series, stats) where `series` is the 1 s pod count array of the window
        and `stats` a dict of scalars.

    Raises:
        ValueError: If the window is not fully covered by the cached span.
    """
    t_start = cache["meta"]["t_start"]
    t_end = cache["meta"]["t_end"]
    if window_start < t_start or window_end > t_end or window_end < window_start:
        raise ValueError(f"window [{window_start},{window_end}] outside cached "
                         f"trace span [{t_start},{t_end}]")
    series = cache["pods_per_s"][window_start - t_start:window_end - t_start + 1]
    span = series.size
    stats = {
        "window_start": int(window_start),
        "window_end": int(window_end),
        "window_span_s": int(span),
        "pods": int(series.sum()),
        "mean_rate": float(series.sum() / span),
        "max_1s_rate": float(series.max()),
        "min_1s_rate": float(series.min()),
        "p50_1s_rate": float(np.percentile(series, 50)),
        "seconds_above_cap": int((series > cap).sum()),
        "mean_over_cap": float(series.sum() / span / cap),
    }
    return series, stats


def scope_series(cache, active):
    """Per-second arrival series of the reporting scope plus its description.

    Args:
        cache:  Cache dict from load_cache()/build_cache().
        active: Active-period info dict (as returned by collect_stats under
                "active_period"), or None for the whole trace span.

    Returns:
        (series, t_start) where `series` is the per-second pod count array of the
        scope and `t_start` its first trace second.
    """
    pods_per_s = cache["pods_per_s"]
    t_start = cache["meta"]["t_start"]
    if not active:
        return pods_per_s, t_start
    i0 = active["head_trimmed_s"]
    i1 = pods_per_s.size - active["tail_trimmed_s"]
    return pods_per_s[i0:i1], t_start + i0


def collect_stats(cache, cap, window_start, window_end, active_kwargs=None,
                  full_span=False):
    """Assemble every number the paper text quotes from this figure.

    Args:
        cache:         Cache dict from load_cache()/build_cache().
        cap:           Single-scheduler reference rate in pods/s.
        window_start:  First second of the representative window.
        window_end:    Last second of the representative window.
        active_kwargs: Overrides forwarded to active_span().
        full_span:     Report the whole span as the primary scope instead of the
                       active period (the active period is still described).

    Returns:
        Nested dict with the trace summary, the active-period rule and its
        boundaries, the window statistics of the primary scope ("windows") and of
        both fixed scopes ("windows_full_span", "windows_active_period") for
        reference, and the representative-window summary.
    """
    meta = cache["meta"]
    pods_per_s = cache["pods_per_s"]
    span = meta["span_s"]
    i0, i1, info = active_span(pods_per_s, **(active_kwargs or {}))
    active_seg = pods_per_s[i0:i1]
    info.update({
        "t_start": meta["t_start"] + i0,
        "t_end": meta["t_start"] + i1 - 1,
        "span_h": (i1 - i0) / 3600.0,
        "pods": int(active_seg.sum()),
        "mean_rate": float(active_seg.sum()) / (i1 - i0),
        "idle_seconds": int((active_seg == 0).sum()),
        "frac_idle_seconds": float((active_seg == 0).mean()),
    })
    scope = pods_per_s if full_span else active_seg
    _, rep = representative_window_stats(cache, window_start, window_end, cap)
    return {
        "cap_pods_per_s": cap,
        "scope": "full-span" if full_span else "active-period",
        "trace": {
            "csv_rows": meta["csv_rows"],
            "task_rows_parsed": meta["total_tasks"],
            "dropped_rows": meta["dropped_rows"],
            "pod_instances": meta["total_pods"],
            "t_start": meta["t_start"],
            "t_end": meta["t_end"],
            "span_s": span,
            "span_h": span / 3600.0,
            "span_days": span / 86400.0,
            "mean_rate": meta["total_pods"] / float(span),
            "idle_seconds": int((pods_per_s == 0).sum()),
            "first_loaded_second": int(meta["t_start"]
                                       + int(np.argmax(pods_per_s > 0))),
        },
        "active_period": info,
        "windows": {str(w): window_stats(scope, w, cap) for w in STAT_WINDOWS},
        "windows_full_span": {str(w): window_stats(pods_per_s, w, cap)
                              for w in STAT_WINDOWS},
        "windows_active_period": {str(w): window_stats(active_seg, w, cap)
                                  for w in STAT_WINDOWS},
        "representative_window": rep,
    }


def _print_window_table(windows, cap):
    """Print one window-rate table (primary or reference scope)."""
    print(f"{'win':>5} {'#win':>9} {'#loaded':>9} {'mean':>10} {'P50':>10} "
          f"{'P90':>10} {'P95':>10} {'P99':>10} {'max':>11} "
          f"{'>cap all':>9} {'>cap load':>10}")
    for key in sorted(windows, key=int):
        s = windows[key]
        print(f"{s['window_s']:>4}s {s['n_windows']:>9,} {s['n_windows_loaded']:>9,} "
              f"{s['mean']:>10,.1f} {s['p50']:>10,.1f} {s['p90']:>10,.1f} "
              f"{s['p95']:>10,.1f} {s['p99']:>10,.1f} {s['max']:>11,.1f} "
              f"{s['frac_above_cap_all'] * 100:>8.1f}% "
              f"{s['frac_above_cap_loaded'] * 100:>9.1f}%")
    print("  (inverted-CDF / \"higher\" percentiles)")
    for key in sorted(windows, key=int):
        s = windows[key]
        print(f"{s['window_s']:>4}s {'':>19} {'':>10} {s['p50_higher']:>10,.1f} "
              f"{s['p90_higher']:>10,.1f} {s['p95_higher']:>10,.1f} "
              f"{s['p99_higher']:>10,.1f}")


def print_stats(stats):
    """Print the statistics block so every plotted number is auditable."""
    tr = stats["trace"]
    ap = stats["active_period"]
    cap = stats["cap_pods_per_s"]
    print("\n=== trace ===")
    print(f"  csv rows            : {tr['csv_rows']:,}")
    print(f"  task rows parsed    : {tr['task_rows_parsed']:,} "
          f"(dropped {tr['dropped_rows']:,})")
    print(f"  pod instances       : {tr['pod_instances']:,}")
    print(f"  span                : [{tr['t_start']:,}, {tr['t_end']:,}] "
          f"= {tr['span_s']:,} s = {tr['span_h']:.2f} h = {tr['span_days']:.2f} d")
    print(f"  idle seconds        : {tr['idle_seconds']:,} "
          f"({tr['idle_seconds'] / tr['span_s'] * 100:.1f}% of span); "
          f"first loaded second = {tr['first_loaded_second']:,}")
    print(f"  mean arrival rate   : {tr['mean_rate']:,.1f} pods/s")

    frac = ap["threshold_frac_of_mean"]
    print("\n=== active period ===")
    print(f"  rule                : drop leading/trailing runs of "
          f"{ap['rule_window_s']:g} s windows below "
          f"{ap['threshold_pods_per_s']:,.1f} pods/s"
          + (f" ({frac * 100:g}% of the trace mean)" if frac else "")
          + f" lasting >= {ap['min_duration_s'] / 3600:g} h")
    print(f"  trimmed             : head {ap['head_trimmed_s'] / 3600:.2f} h, "
          f"tail {ap['tail_trimmed_s'] / 3600:.2f} h, "
          f"{ap['pods_excluded']:,} pods "
          f"({ap['frac_pods_excluded'] * 100:.4f}% of all arrivals)")
    print(f"  active span         : [{ap['t_start']:,}, {ap['t_end']:,}] "
          f"= {ap['active_span_s']:,} s = {ap['span_h']:.2f} h")
    print(f"  pods / mean rate    : {ap['pods']:,} / {ap['mean_rate']:,.1f} pods/s")
    print(f"  idle seconds inside : {ap['idle_seconds']:,} "
          f"({ap['frac_idle_seconds'] * 100:.2f}%)")

    print(f"\n=== window rate distribution, scope = {stats['scope']} "
          f"(cap = {cap:g} pods/s) ===")
    _print_window_table(stats["windows"], cap)
    print("\n=== window rate distribution, whole span (reference) ===")
    _print_window_table(stats["windows_full_span"], cap)

    rep = stats["representative_window"]
    print(f"\n=== representative window [{rep['window_start']:,}, "
          f"{rep['window_end']:,}] ===")
    print(f"  pods                : {rep['pods']:,}")
    print(f"  mean rate           : {rep['mean_rate']:,.1f} pods/s "
          f"({rep['mean_over_cap']:.1f}x cap)")
    print(f"  1 s rate min/P50/max: {rep['min_1s_rate']:,.0f} / "
          f"{rep['p50_1s_rate']:,.0f} / {rep['max_1s_rate']:,.0f} pods/s")
    print(f"  seconds above cap   : {rep['seconds_above_cap']}/"
          f"{rep['window_span_s']}")



# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="Directory holding batch_task.csv")
    parser.add_argument("--cache",
                        help="Replot cache path (default: <cache-dir>/" + CACHE_NAME + ")")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help="Where the per-second cache lives")
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="Re-stream the trace even if the cache exists")
    parser.add_argument("--cap", type=float, default=DEFAULT_CAP,
                        help="Single-scheduler reference rate in pods/s")
    parser.add_argument("--window-start", type=int, default=WINDOW_START,
                        help="First trace second of the representative window")
    parser.add_argument("--window-end", type=int, default=WINDOW_END,
                        help="Last trace second of the representative window")
    parser.add_argument("--cdf-windows", type=int, nargs="+",
                        default=list(CDF_WINDOWS),
                        help="Window sizes the CDF panel draws")
    parser.add_argument("--full-span", action="store_true",
                        help="Report the whole trace instead of the active period")
    parser.add_argument("--active-min-rate", type=float,
                        help="Absolute threshold, overriding the fraction rule")
    parser.add_argument("--active-min-rate-frac", type=float,
                        default=ACTIVE_MIN_RATE_FRAC)
    parser.add_argument("--active-min-duration", type=float,
                        default=ACTIVE_MIN_DURATION)
    parser.add_argument("--active-window", type=int, default=ACTIVE_WINDOW)
    parser.add_argument("--export-csv", action="store_true",
                        help="Also write the per-second and per-minute series as CSV")
    parser.add_argument("--quiet", action="store_true",
                        help="Do not print the statistics block")
    args = parser.parse_args(argv)

    cache_dir = os.path.abspath(args.cache_dir)
    cache_path = args.cache or os.path.join(cache_dir, CACHE_NAME)
    if args.rebuild_cache or not os.path.exists(cache_path):
        cache = build_cache(args.data_dir, cache_path)
    else:
        cache = load_cache(cache_path)
        print("[cache] " + cache_path + " (built "
              + str(cache["meta"].get("generated_utc", "n/a")) + ")")

    active_kwargs = {
        "min_rate": args.active_min_rate,
        "min_rate_frac": args.active_min_rate_frac,
        "min_duration": args.active_min_duration,
        "window": args.active_window,
    }
    stats = collect_stats(cache, args.cap, args.window_start, args.window_end,
                          active_kwargs=active_kwargs, full_span=args.full_span)
    if not args.quiet:
        print_stats(stats)

    if args.export_csv:
        for path in export_series_csv(cache, cache_dir):
            print("Wrote " + path)

    # The CDF panel reads the whole reporting scope; the window panel reads only
    # its own minute, so the slice travels with the data rather than the figure
    # re-deriving it from a series it would otherwise not need.
    cdf_series, _ = scope_series(
        cache, None if args.full_span else stats["active_period"])
    window_series, _ = representative_window_stats(
        cache, args.window_start, args.window_end, args.cap)

    payload = {
        "cap": args.cap,
        "cdf_windows": list(args.cdf_windows),
        "cdf_title": "(a) Whole trace" if args.full_span else "(a) Active period",
        # The CDF panel draws one curve per window size. Binning is a data
        # reduction, so it happens here and the figure only draws the result.
        "cdf_rates": {
            str(window): [float(value) for value
                          in bin_counts(cdf_series, window) / float(window)]
            for window in args.cdf_windows
        },
        "scope_seconds": int(cdf_series.size),
        "window": {
            "start": int(args.window_start),
            "end": int(args.window_end),
            "series": [int(value) for value in window_series],
        },
        "stats": stats,
    }
    path = envelope.write(
        envelope.path_for(_EXPERIMENTS, FIGURE),
        figure=FIGURE, source="trace", data=payload, generator=GENERATOR,
        boards=["G"],
        notes="Alibaba cluster-trace-v2018; the trace is not redistributed.",
    )
    print("wrote " + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
