#!/usr/bin/env python3
"""
Paper figure: Alibaba cluster-trace-v2018 task-instance start rate.

Layout (default 1 row x 2 columns, 3.33" wide == \\columnwidth of the ACM
sigplan style; `--cdf-only` remains available for diagnostic replots):
  Left  panel: CDF over all fixed-size windows of the reporting scope (active
               period by default; whole trace span only with --full-span), one
               curve per window size (1 s and 60 s by default). A vertical rule
               marks the approximately 200/s single-scheduler reference from the
               G\"odel paper, and each legend entry reports the share of windows
               above it.
  Right panel: 1 s-resolution time series of one representative 60 s window
               (the previously selected near-median whole-span window
               627120..627179 pinned by analyze_trace.py)
               on a linear axis. Horizontal rules mark the same 200/s reference
               and this window's mean task-instance start rate.

The rate is a *task-instance start-rate proxy*: each accepted `batch_task` row
contributes `instance_num` arrivals at `floor(start_time)`. It is neither a
measured per-instance start time nor a Kubernetes pod submission time, so the
figure characterizes workload intensity, not a replayable arrival trace.

Arrival semantics are taken verbatim from analyze_trace.py (the reference
single-pass scan of this repository), so the numbers behind both panels are
directly comparable with the numbers reported there:

  batch_task.csv row = task_name, instance_num, job_name, task_type, status,
                       start_time, end_time, plan_cpu, plan_mem
  arrival second     = floor(start_time)            (column 6, 0-based index 5)
  pods per task row  = int(instance_num)            (column 2, 0-based index 1)
  dropped rows       = fewer than 9 fields, or non-numeric / NaN / <= 0
                       start_time, or non-numeric / NaN / <= 0 instance_num
  trace span         = [min arrival second, max arrival second], empty seconds
                       inside the span included
  window rate        = pods falling in the window / window size in seconds
                       (the last window of the span may be partial; its rate is
                       still divided by the full window size, as in
                       analyze_trace.py)

Active period (default reporting scope, see active_span()): the leading and
trailing stretches in which the 60 s arrival rate stays below 10% of the trace
mean rate for at least one hour are collection ramp-in / ramp-out, not workload,
so they are excluded from the window population. Only the two ends are trimmed;
empty windows *inside* the active period stay in the statistics. `--full-span`
restores the whole-span scope.

Data (not in the repository, ~124 MiB compressed / 765 MiB expanded):

    url=http://aliopentrace.oss-cn-beijing.aliyuncs.com/v2018Traces   # fetchData.sh
    wget -c $url/batch_task.tar.gz && tar xzf batch_task.tar.gz
    # sha256 7c4b32361bd1ec2083647a8f52a6854a03bc125ca5c202652316c499fbf978c6
    # (checksums are listed in bin/clusterdata/cluster-trace-v2018/trace_2018.md)

Because the 765 MiB CSV scan takes minutes, the per-second and per-60 s arrival
series are cached in a compressed .npz next to the figures. The cache is rebuilt
automatically from <data-dir>/batch_task.csv whenever it is missing, and re-used
otherwise, so re-styling the figure never needs the raw trace again:

    # first run (or after deleting the cache): parse batch_task.csv, then plot
    python plot_arrival_figure.py --data-dir bin/clusterdata/cluster-trace-v2018/data

    # later runs: replot straight from the cache
    python plot_arrival_figure.py

Outputs:
    <output-dir>/fig-arrival-rate.{pdf,svg,png}
    <output-dir>/fig-arrival-rate-stats.json     (every number quoted in the text)
    <output-dir>/arrival-rate-series.npz         (replot cache)
    <paper-figs-dir>/trace-arrival-rate.pdf      (copy consumed by the paper)

Colours, RC parameters and the save convention follow
experiments/scripts/plot-fig-scalability-2panel.py and
experiments/scripts/plot-fig-occupancy-intervals.py (pdf.fonttype=42 keeps the
text as embedded TrueType subsets, so pdflatex needs no external font).
"""

import argparse
import csv
import gzip
import json
import math
import os
import shutil
import sys
import time
from collections import Counter

import matplotlib

matplotlib.use("Agg")
import matplotlib.lines as mlines  # noqa: E402
import matplotlib.patheffects as path_effects  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import seaborn as sns  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import (FuncFormatter, LogLocator, MultipleLocator,  # noqa: E402
                               NullFormatter)

# ---------------------------------------------------------------------------
#  Paths / defaults
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))

# Reuse the arrival semantics of the reference scan instead of re-deriving them.
sys.path.insert(0, _HERE)
from analyze_trace import WINDOW_END, WINDOW_START, num  # noqa: E402

DEFAULT_DATA_DIR = os.path.join(_REPO, "bin", "clusterdata",
                                "cluster-trace-v2018", "data")
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "figures")
DEFAULT_PAPER_FIG_DIR = os.path.join(_REPO, "paper", "figs")
DEFAULT_STEM = "fig-arrival-rate"
DEFAULT_PAPER_STEM = "trace-arrival-rate"
CACHE_NAME = "arrival-rate-series.npz"

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

# ---------------------------------------------------------------------------
#  Colours — same palette as the other paper figures
# ---------------------------------------------------------------------------
_rdbu = sns.color_palette("RdBu", 11)

C_RED_D = "#{:02x}{:02x}{:02x}".format(       # dark red  — cap / reference rules
    int(_rdbu[1][0] * 255), int(_rdbu[1][1] * 255), int(_rdbu[1][2] * 255))
C_BLUE_D = "#{:02x}{:02x}{:02x}".format(      # dark blue — 1 s windows
    int(_rdbu[9][0] * 255), int(_rdbu[9][1] * 255), int(_rdbu[9][2] * 255))
C_GREEN_D = "#238b45"                          # dark green  — 60 s windows
C_GREY = "#999999"                             # neutral annotations

WINDOW_STYLE = {
    1:  {"color": C_BLUE_D,  "ls": "-",  "label": "1 s windows",
         "short": "1 s"},
    10: {"color": C_GREY,    "ls": "-.", "label": "10 s windows",
         "short": "10 s"},
    60: {"color": C_GREEN_D, "ls": "--", "label": "60 s windows",
         "short": "60 s"},
}

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":        8,
    "axes.labelsize":   7.5,
    "axes.titlesize":   8,
    "xtick.labelsize":  6.8,
    "ytick.labelsize":  6.8,
    "legend.fontsize":  6.8,
    "axes.linewidth":   0.7,
    "lines.linewidth":  1.1,
    "xtick.major.size": 2.2,
    "ytick.major.size": 2.2,
    "xtick.major.pad":  1.6,
    "ytick.major.pad":  1.6,
    "pdf.fonttype":     42,
    "ps.fonttype":      42,
    # Arial for text and mathtext alike, so this figure keeps the same family
    # as the rest of the paper rather than inheriting it from seaborn.
    "font.family":      "sans-serif",
    "font.sans-serif":  ["Arial", "Helvetica", "DejaVu Sans"],
    "mathtext.fontset": "custom",
    "mathtext.rm":      "Arial",
    "mathtext.it":      "Arial:italic",
    "mathtext.bf":      "Arial:bold",
}

ANNOT_FONTSIZE = 6.6   # >= 6.5 pt so the figure stays readable at column width
LW_CURVE = 1.15
LW_RULE = 0.9

# White outline keeps in-panel annotations legible where they cross a curve.
LABEL_HALO = path_effects.withStroke(linewidth=1.5, foreground="white")


# ---------------------------------------------------------------------------
#  Trace scan (identical field / validity semantics as analyze_trace.py)
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
                      "same validity filter as analyze_trace.analyze_batch_tasks"),
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
        the inverted-CDF ("higher") convention of analyze_trace.weighted_quantiles,
        which is the convention behind the values recorded in
        paper/review-rebuttal/trace-study.md.
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
    print("  (inverted-CDF / \"higher\" percentiles: convention of trace-study.md)")
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
#  Plotting
# ---------------------------------------------------------------------------

def _nice_step(span, target=4):
    """Tick step covering `span` in about `target` intervals with round labels.

    Args:
        span:   Axis range in data units (> 0).
        target: Preferred number of intervals.

    Returns:
        A 1 / 2 / 2.5 / 5 x 10^k step; slightly larger steps are preferred over
        awkward ones, so a 9,000-wide axis ticks every 2,000 rather than 2,250.
    """
    if span <= 0:
        return 1.0
    raw = span / float(target)
    exp = math.floor(math.log10(raw))
    for candidate in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = candidate * (10 ** exp)
        if step >= raw * 0.75:
            return step
    return 10.0 ** (exp + 1)


def _compact_rate(value, _pos=None):
    """Tick formatter: 8000 -> "8k", 500 -> "500" (narrow labels fit the panel).

    Args:
        value: Tick value in pods/s.
        _pos:  Tick position, ignored (matplotlib FuncFormatter signature).

    Returns:
        Formatted tick label.
    """
    if value >= 1000 and value % 1000 == 0:
        return f"{value / 1000:.0f}k"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def _cdf_points(rates, x_floor):
    """Step-CDF support points, with sub-`x_floor` rates folded onto `x_floor`.

    A log x axis cannot show windows with no arrivals at all, so their
    probability mass is kept but drawn at the left edge: the curve starts at
    (x_floor, P[rate <= x_floor]) and is exact for every rate > x_floor.

    Args:
        rates:   Window rates in pods/s (all windows of the reporting scope).
        x_floor: Left edge of the log axis in pods/s.

    Returns:
        (x, y) arrays for a `where="post"` step plot.
    """
    clipped = np.maximum(rates, x_floor)
    vals, counts = np.unique(clipped, return_counts=True)
    cum = np.cumsum(counts) / rates.size
    return vals, cum


def _draw_cdf_panel(ax, series, cap, windows, x_floor):
    """Left panel: CDF of window arrival rates + above-cap annotation.

    Args:
        ax:       Target axes.
        series:   Per-second arrivals of the reporting scope (active period by
                  default, whole span with --full-span).
        cap:      Single-scheduler reference rate in pods/s.
        windows:  Window sizes to draw.
        x_floor:  Left edge of the log x axis in pods/s.

    Every window of the scope is part of the population, including empty windows
    inside it; only the quiet head and tail of the trace are excluded upstream.
    """
    rates_by_w = {w: bin_counts(series, w) / float(w) for w in windows}
    x_max = max(r.max() for r in rates_by_w.values())
    x_right = x_max * 1.7

    handles = []
    for w in windows:
        style = WINDOW_STYLE[w]
        rates = rates_by_w[w]
        x, y = _cdf_points(rates, x_floor)
        # Extend the last step to the right edge so the curve reaches 1.
        x = np.append(x, x_right)
        y = np.append(y, y[-1])
        ax.step(x, y, where="post", color=style["color"], ls=style["ls"],
                lw=LW_CURVE, solid_joinstyle="round", zorder=3)
        frac = float((rates > cap).mean())
        handles.append(mlines.Line2D(
            [], [], color=style["color"], ls=style["ls"], lw=LW_CURVE,
            label=f"{style['short']}: {frac * 100:.0f}% above"))

    ax.axvline(cap, color=C_RED_D, ls=":", lw=LW_RULE, zorder=2)
    ax.set_xscale("log")
    ax.set_xlim(x_floor, x_right)
    ax.set_ylim(0, 1.04)
    # The series expands each task row by its instance count at the task start
    # second, so the axis is a task-instance start-rate proxy, not a measured
    # per-instance (let alone per-pod) submission rate.
    ax.set_xlabel("Arrival rate (pods/s)", labelpad=1.0)
    ax.set_ylabel("CDF of windows", labelpad=1.5)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["0", ".25", ".5", ".75", "1"])
    ax.xaxis.set_major_locator(LogLocator(base=10, numticks=8))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.grid(True, which="major", ls=":", lw=0.4, alpha=0.4)

    # Cap rule label, kept above the steep part of both curves.
    ax.text(cap * 0.72, 0.955, f"{cap:g} pods/s", color=C_RED_D,
            fontsize=ANNOT_FONTSIZE, ha="right", va="top", rotation=90,
            path_effects=[LABEL_HALO], zorder=5)
    # Legend doubles as the above-cap annotation (one share per window size); it
    # is anchored at the very bottom so it stays clear of the CDF plateaus.
    legend = ax.legend(handles=handles, loc="lower right", frameon=False,
                       handlelength=1.4, handletextpad=0.35, labelspacing=0.15,
                       borderpad=0.0, borderaxespad=0.15, alignment="left")
    for text in legend.get_texts():
        text.set_path_effects([LABEL_HALO])


def _draw_window_panel(ax, cache, cap, window_start, window_end, log_y):
    """Right panel: 1 s arrival series of the representative window.

    The default linear y axis is deliberate: it shows at a glance how far the
    per-second arrival rate sits above the single-scheduler reference, which a
    log axis would compress. A linear axis also represents the seconds with zero
    arrivals exactly. Under the optional log scale (`--log-window-y`) those
    zeros are drawn at the axis floor instead, since a log axis cannot show them.

    Args:
        ax:           Target axes.
        cache:        Cache dict from load_cache()/build_cache().
        cap:          Single-scheduler reference rate in pods/s.
        window_start: First trace second of the window (inclusive).
        window_end:   Last trace second of the window (inclusive).
        log_y:        Use a log y axis instead of the linear default.

    Returns:
        The representative-window statistics dict.
    """
    series, stats = representative_window_stats(cache, window_start, window_end,
                                                cap)
    t = np.arange(series.size)

    if log_y:
        y_floor = 0.8
        y_top = series.max() * 4.2
        y = np.maximum(series, y_floor)
    else:
        y_floor = 0.0
        # Headroom for the rule legend above the peak, with the top rounded to a
        # whole 500 and ticks on a "nice" step so the labels read cleanly.
        y_top = 500.0 * math.ceil(series.max() * 1.15 / 500.0)
        y_step = _nice_step(y_top)
        y = series.astype(float)

    ax.plot(t, y, color=C_BLUE_D, lw=LW_CURVE, zorder=4)
    h_mean = ax.axhline(stats["mean_rate"], color=C_GREY, ls="--", lw=LW_RULE,
                        zorder=3, label=f"mean {stats['mean_rate']:,.0f} pods/s")
    h_cap = ax.axhline(cap, color=C_RED_D, ls=":", lw=LW_RULE, zorder=3,
                       label=f"{cap:g} pods/s")

    ax.set_xlim(-0.6, series.size - 0.4)
    ax.set_xticks([0, 20, 40, 60])
    # The panel sits at the right edge of a single-column figure, so a label
    # wide enough to also name the window start (t=627,120) overflows the saved
    # tight bounding box. The absolute start second stays in the stats JSON and
    # in analyze_trace.WINDOW_START rather than on the axis.
    ax.set_xlabel("Trace time (s)", labelpad=1.0)
    ax.set_ylabel("Arrival rate (pods/s)", labelpad=1.5)
    ax.grid(True, which="major", ls=":", lw=0.4, alpha=0.4)

    if log_y:
        ax.set_yscale("log")
        ax.set_ylim(y_floor, y_top)
        ax.yaxis.set_major_locator(LogLocator(base=10, numticks=6))
        ax.yaxis.set_minor_formatter(NullFormatter())
        # On a log axis both rules are far apart, so label them in place.
        ax.text(0.5, stats["mean_rate"] * 1.35,
                f"mean {stats['mean_rate']:,.0f}/s",
                color="#4d4d4d", fontsize=ANNOT_FONTSIZE, ha="left", va="bottom",
                path_effects=[LABEL_HALO], zorder=5)
        ax.text(series.size - 1.0, cap / 1.30,
                f"{cap:g}/s scheduler", color=C_RED_D,
                fontsize=ANNOT_FONTSIZE, ha="right", va="top",
                path_effects=[LABEL_HALO], zorder=5)
    else:
        ax.set_ylim(0, y_top)
        ax.yaxis.set_major_locator(MultipleLocator(y_step))
        ax.yaxis.set_major_formatter(FuncFormatter(_compact_rate))
        # The cap sits close to the x axis here, so in-place labels would
        # collide with it and with the curve; a frameless legend in the empty
        # upper-right corner keeps both rules identifiable.
        legend = ax.legend(handles=[h_mean, h_cap], loc="upper right",
                           frameon=False, handlelength=1.3, handletextpad=0.35,
                           labelspacing=0.12, borderpad=0.0, borderaxespad=0.15,
                           alignment="left")
        for text, colour in zip(legend.get_texts(), ("#4d4d4d", C_RED_D)):
            text.set_color(colour)
            text.set_path_effects([LABEL_HALO])
    return stats


def build_figure(cache, cap, windows, window_start, window_end, width, height,
                 x_floor, log_y, panel_titles, cdf_series=None,
                 cdf_title="(a) Active period", cdf_only=False):
    """Assemble the single-column figure (CDF panel, optionally + window panel).

    Args:
        cache:        Cache dict from load_cache()/build_cache().
        cap:          Single-scheduler reference rate in pods/s.
        windows:      Window sizes drawn in the CDF panel.
        window_start: First second of the representative window.
        window_end:   Last second of the representative window.
        width:        Figure width in inches (3.33 == sigplan \\columnwidth).
        height:       Figure height in inches.
        x_floor:      Left edge of the CDF log axis in pods/s.
        log_y:        Whether the time-series panel uses a log y axis.
        panel_titles: Whether to print the "(a)"/"(b)" panel titles.
        cdf_series:   Per-second arrivals feeding the CDF panel; defaults to the
                      whole cached span.
        cdf_title:    Title of the CDF panel, naming its reporting scope.
        cdf_only:     Draw only the CDF panel at full figure width. This remains
                      useful for diagnostics; the paper uses the default two-panel
                      layout so readers can see a representative burst window.

    Returns:
        The matplotlib Figure.
    """
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    fig = plt.figure(figsize=(width, height))
    top = 1.0 - (0.20 if panel_titles else 0.06) / height
    n_cols = 1 if cdf_only else 2
    gs = GridSpec(1, n_cols, figure=fig, wspace=0.52,
                  left=0.135 if not cdf_only else 0.125,
                  right=0.985, top=top, bottom=0.30 / height)
    ax_cdf = fig.add_subplot(gs[0, 0])

    series = cache["pods_per_s"] if cdf_series is None else cdf_series
    _draw_cdf_panel(ax_cdf, series, cap, windows, x_floor)
    if not cdf_only:
        ax_win = fig.add_subplot(gs[0, 1])
        _draw_window_panel(ax_win, cache, cap, window_start, window_end, log_y)

    if panel_titles:
        # Regular weight, matching the panel titles of the other paper figures.
        ax_cdf.set_title(cdf_title, fontsize=RC_PARAMS["axes.titlesize"],
                         loc="left", pad=2)
        if not cdf_only:
            ax_win.set_title("(b) 60 s window",
                             fontsize=RC_PARAMS["axes.titlesize"],
                             loc="left", pad=2)
    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig, output_dir, stem, paper_fig_dir, paper_stem):
    """Write PDF/SVG/PNG and copy the PDF to the paper figure directory.

    Args:
        fig:           Figure to save.
        output_dir:    Directory for the PDF/SVG/PNG outputs.
        stem:          File name stem inside `output_dir`.
        paper_fig_dir: Directory receiving the PDF copy; skipped when empty.
        paper_stem:    File name stem of the paper copy.

    Returns:
        List of written paths.
    """
    os.makedirs(output_dir, exist_ok=True)
    written = []
    for ext in ("pdf", "svg", "png"):
        path = os.path.join(output_dir, f"{stem}.{ext}")
        fig.savefig(path, dpi=400, bbox_inches="tight", pad_inches=0.012)
        written.append(path)
    if paper_fig_dir:
        os.makedirs(paper_fig_dir, exist_ok=True)
        target = os.path.join(paper_fig_dir, f"{paper_stem}.pdf")
        shutil.copyfile(os.path.join(output_dir, f"{stem}.pdf"), target)
        written.append(target)
    return written


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Paper figure — Alibaba cluster-trace-v2018 pod arrival rate "
                    "(CDF over the active period + one representative 60 s "
                    "window)")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="Directory holding batch_task.csv; it is scanned "
                         "automatically whenever the replot cache is missing")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                    help="Directory for the figure, the stats JSON and the cache")
    ap.add_argument("--paper-figs-dir", default=DEFAULT_PAPER_FIG_DIR,
                    help="Directory receiving the PDF copy; empty string disables")
    ap.add_argument("--cache", default=None,
                    help=f"Replot cache path (default <output-dir>/{CACHE_NAME})")
    ap.add_argument("--rebuild-cache", action="store_true",
                    help="Rescan batch_task.csv even if the cache exists")
    ap.add_argument("--export-csv", action="store_true",
                    help="Also export the 1 s / 60 s rate series as csv.gz")
    ap.add_argument("--window-start", type=int, default=WINDOW_START,
                    help="First trace second of the representative window")
    ap.add_argument("--window-end", type=int, default=WINDOW_END,
                    help="Last trace second of the representative window")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP,
                    help="Single-scheduler reference rate in pods/s")
    ap.add_argument("--full-span", action="store_true",
                    help="Report and plot every window of the whole trace span "
                         "instead of the active period (keeps the collection "
                         "ramp-in / ramp-out in the population)")
    ap.add_argument("--active-min-rate", type=float, default=None,
                    help="Active-period threshold in pods/s; overrides "
                         "--active-min-rate-frac")
    ap.add_argument("--active-min-rate-frac", type=float,
                    default=ACTIVE_MIN_RATE_FRAC,
                    help="Active-period threshold as a fraction of the trace "
                         "mean rate (default 0.10 = 184 pods/s)")
    ap.add_argument("--active-min-duration", type=float,
                    default=ACTIVE_MIN_DURATION,
                    help="Minimum length in seconds of a trimmed head/tail run")
    ap.add_argument("--active-window", type=int, default=ACTIVE_WINDOW,
                    help="Window size in seconds the active-period rule uses")
    ap.add_argument("--cdf-windows", type=int, nargs="+", default=list(CDF_WINDOWS),
                    choices=sorted(WINDOW_STYLE), help="Window sizes in the CDF")
    ap.add_argument("--width", type=float, default=3.33,
                    help="Figure width in inches (3.33 = sigplan column width)")
    ap.add_argument("--height", type=float, default=1.62,
                    help="Figure height in inches")
    ap.add_argument("--cdf-x-floor", type=float, default=1.0,
                    help="Left edge of the CDF log axis in pods/s")
    ap.add_argument("--log-window-y", action="store_true",
                    help="Use a log y axis in the time-series panel instead of "
                         "the linear default")
    ap.add_argument("--no-panel-titles", action="store_true",
                    help="Drop the (a)/(b) panel titles")
    ap.add_argument("--cdf-only", action="store_true",
                    help="Draw only the CDF panel at the full figure width")
    ap.add_argument("--stem", default=DEFAULT_STEM)
    ap.add_argument("--paper-stem", default=DEFAULT_PAPER_STEM)
    args = ap.parse_args(argv)

    output_dir = os.path.abspath(args.output_dir)
    cache_path = args.cache or os.path.join(output_dir, CACHE_NAME)

    if args.rebuild_cache or not os.path.exists(cache_path):
        cache = build_cache(args.data_dir, cache_path)
    else:
        cache = load_cache(cache_path)
        print(f"[cache] {cache_path} "
              f"(built {cache['meta'].get('generated_utc', 'n/a')})")

    active_kwargs = {
        "min_rate": args.active_min_rate,
        "min_rate_frac": args.active_min_rate_frac,
        "min_duration": args.active_min_duration,
        "window": args.active_window,
    }
    stats = collect_stats(cache, args.cap, args.window_start, args.window_end,
                          active_kwargs=active_kwargs, full_span=args.full_span)
    print_stats(stats)

    stats_path = os.path.join(output_dir, f"{args.stem}-stats.json")
    os.makedirs(output_dir, exist_ok=True)
    with open(stats_path, "w") as f:
        json.dump(stats, f, indent=2, sort_keys=True)

    if args.export_csv:
        for p in export_series_csv(cache, output_dir):
            print(f"Wrote {p}")

    cdf_series, _ = scope_series(cache, None if args.full_span
                                 else stats["active_period"])
    cdf_title = ("(a) Whole trace" if args.full_span else "(a) Active period")
    fig = build_figure(cache, args.cap, args.cdf_windows, args.window_start,
                       args.window_end, args.width, args.height,
                       args.cdf_x_floor, args.log_window_y,
                       not args.no_panel_titles, cdf_series=cdf_series,
                       cdf_title=cdf_title, cdf_only=args.cdf_only)
    written = save(fig, output_dir, args.stem, args.paper_figs_dir,
                   args.paper_stem)
    plt.close(fig)

    print(f"\nWrote {stats_path}")
    for path in written:
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
