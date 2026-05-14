#!/usr/bin/env python3
"""
Generate paper figures from cluster experiment results.

Data source layout (auto-scanned from --results-root):
    results/
        B1/summary.json          # B1-{1000,2000,5000}n-{E1,E2,E3}
        B2/summary.json          # B2-{2000,5000,10000,20000}n-{E1..E3,P1..P4}
        B3/summary.json          # B3-N{2,4,6,8,10}-{E2,E3,P1,P4}
        ablation/summary.json    # AbE{0..3}, AbP{0..3}
        K/summary.json           # S-{E,P}-K{0,1,2,4}
        strategy/summary.json    # S-{E,P}-Strategy-<name>-<sync>
        P/summary.json           # S-{E,P}-P{00,01,03,05,07}-{glob,same,diff}

Figures (matches experiments/analysis-report.md §10.3):
    Fig-B1               Low-contention scale (E1/E2/E3, 1k/2k)
    Fig-B2a / Fig-B2b    HC-V scale: throughput / ACF
    Fig-B3a / Fig-B3b    Scheduler-count: throughput / ACF
    Fig-C-Event          Event-driven ablation (AbE0..AbE3)
    Fig-C-Periodic       Periodic ablation (AbP0..AbP3)
    Fig-P                ParSync paper strategy reproduction
    Fig-Paradigm-Cost    E2/E3/P1/P4 cost comparison at 10000n
    Fig-Sens-K           candidate_k sensitivity (Event/Periodic)
    Fig-Sens-p           penalty_weight sensitivity (Event/Periodic)

Statistics: median primary, IQR (q1..q3) as asymmetric error.
"""

import argparse
import json
import math
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import seaborn as sns

# ═════════════════════════════════════════════════════════════════════════════
#  Style constants
# ═════════════════════════════════════════════════════════════════════════════

sns.set_style("ticks")
rdbu = sns.color_palette("RdBu", 11)

COLOR = {
    "E1": "#888888",
    "E2": rdbu[2],   # warm — vanilla event
    "E3": rdbu[9],   # cool — proposed event
    "P1": rdbu[1],   # warm — glob vanilla
    "P2": rdbu[3],
    "P3": rdbu[0],   # darkest warm — diff vanilla (worst)
    "P4": rdbu[8],   # cool — proposed periodic
    # Ablation
    "Ab0": rdbu[1],
    "Ab1": rdbu[3],
    "Ab2": rdbu[7],
    "Ab3": rdbu[9],
    # ParSync strategies — use distinct hues so LF doesn't blend into background
    "QF":     "#1f77b4",   # blue
    "WR":     "#17becf",   # cyan
    "LF":     "#9467bd",   # purple
    "QF-PS":  "#d62728",   # red — paper QF failure mode anchor
    "LF-PS":  "#ff7f0e",   # orange
}

MARKER = {
    "E1": "^", "E2": "o", "E3": "s",
    "P1": "D", "P2": "v", "P3": "o", "P4": "s",
}

LINESTYLE = {
    "E1": "-.", "E2": "--", "E3": "-",
    "P1": "--", "P2": "--", "P3": "--", "P4": "-",
}

LABEL = {
    "E1": "Single sched.",
    "E2": "Vanilla (event)",
    "E3": "Proposed (event)",
    "P1": "globSync (vanilla)",
    "P2": "sameSync",
    "P3": "diffSync (vanilla)",
    "P4": "Proposed (periodic)",
}

# Layout
FIG_SINGLE = (4.5, 3.2)
FIG_DUAL = (9.0, 3.2)
FIG_QUAD = (12.0, 3.2)
GRID_KW = dict(linestyle="--", alpha=0.5)
FILL_ALPHA = 0.12
BAR_CAP = 3
OUTPUT_FMTS = ("pdf", "svg", "png")


# ═════════════════════════════════════════════════════════════════════════════
#  Data loading & aggregation
# ═════════════════════════════════════════════════════════════════════════════

def load_all(results_root):
    """Scan results_root/*/summary.json and merge into one record list."""
    records = []
    for sub in sorted(os.listdir(results_root)):
        path = os.path.join(results_root, sub, "summary.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            records.extend(data)
            print(f"  + {sub}/summary.json  ({len(data)} trials)")
    return records


# Trials excluded explicitly per analysis-report.md §9 (data quality issues).
SKIP_EXPERIMENTS = {
    # B1-5000n: all SLO_Fail (145k pod × 5s e2e SLO exceeded test API-server)
    "B1-5000n-E1", "B1-5000n-E2", "B1-5000n-E3",
    # B2-5000n-P*: metrics holes (algo_p99/cpu = NaN, acf=0 anomaly)
    "B2-5000n-P1", "B2-5000n-P2", "B2-5000n-P3", "B2-5000n-P4",
}


def is_valid_trial(r):
    """Drop trials with no usable data.

    Two record shapes:
      - Scheduling boards (B/C/A): require scheduled_pods > 0
      - D board (application-level): require nginx_qps > 0
    """
    exp = r.get("experiment", "")
    if exp.startswith("D"):
        return (r.get("nginx_qps") or 0) > 0
    sp = r.get("scheduled_pods")
    return sp is not None and sp > 0


def group_records(records):
    """Group by experiment name, keep only valid trials, drop skip-list."""
    groups = defaultdict(list)
    for r in records:
        exp = r.get("experiment")
        if not exp or exp in SKIP_EXPERIMENTS:
            continue
        if not is_valid_trial(r):
            continue
        groups[exp].append(r)
    return groups


def _clean_vals(vals, filter_fn=None):
    out = []
    for v in vals:
        if v is None:
            continue
        if isinstance(v, bool):  # avoid bool slipping through
            continue
        if not isinstance(v, (int, float)):
            continue
        if not math.isfinite(v):
            continue
        if filter_fn and not filter_fn(v):
            continue
        out.append(float(v))
    return out


def agg(groups, name, field, filter_fn=None):
    """Aggregate a metric across trials.

    Returns dict with: median, q1, q3, mean, std, n.
    All values None if no valid trials.
    """
    trials = groups.get(name, [])
    vals = _clean_vals([t.get(field) for t in trials], filter_fn)
    if not vals:
        return {"median": None, "q1": None, "q3": None,
                "mean": None, "std": None, "n": 0}
    a = np.array(vals)
    return {
        "median": float(np.median(a)),
        "q1": float(np.percentile(a, 25)),
        "q3": float(np.percentile(a, 75)),
        "mean": float(np.mean(a)),
        "std": float(np.std(a, ddof=1)) if len(a) >= 2 else 0.0,
        "n": int(len(a)),
    }


def algo_filter(v):
    """Filter algo_p99 measurement artifact (~2ms is broken metric)."""
    return v > 5.0


# ═════════════════════════════════════════════════════════════════════════════
#  Plotting primitives
# ═════════════════════════════════════════════════════════════════════════════

def _line_arrays(stats):
    """stats: list of dicts → (x_keep, median, lo_err, hi_err)."""
    med, lo, hi = [], [], []
    for s in stats:
        if s["median"] is None:
            med.append(np.nan); lo.append(0); hi.append(0)
        else:
            med.append(s["median"])
            lo.append(s["median"] - s["q1"])
            hi.append(s["q3"] - s["median"])
    return np.array(med), np.array(lo), np.array(hi)


def plot_line_iqr(ax, x, stats, color, label, marker="o", ls="-"):
    """Median line + IQR fill_between."""
    med, lo, hi = _line_arrays(stats)
    mask = np.isfinite(med)
    if not mask.any():
        return
    xa = np.array(x, dtype=float)
    ax.plot(xa[mask], med[mask], color=color, ls=ls, marker=marker,
            markersize=5, label=label, lw=1.6)
    ax.fill_between(xa[mask], (med - lo)[mask], (med + hi)[mask],
                    color=color, alpha=FILL_ALPHA)


def plot_bars_iqr(ax, x_pos, stats, color, label=None, hatch=None, width=0.8):
    """Single-series bar with median + asymmetric IQR error."""
    med, lo, hi = _line_arrays(stats)
    yerr = np.vstack([lo, hi])
    mask = np.isfinite(med)
    bars = ax.bar(np.array(x_pos)[mask], med[mask], width=width,
                  yerr=yerr[:, mask], capsize=BAR_CAP,
                  color=color, hatch=hatch, label=label, edgecolor="white")
    return bars, med, mask


def annotate_bars(ax, x_pos, stats, fmt="{:.1f}", offset_frac=0.025, fontsize=7):
    """Write median value above each bar's upper IQR error cap."""
    ymin, ymax = ax.get_ylim()
    pad = (ymax - ymin) * offset_frac
    new_top = ymax
    for x, s in zip(x_pos, stats):
        if s["median"] is None:
            continue
        upper = s["median"] + max(s["q3"] - s["median"], 0)
        ytext = upper + pad
        ax.text(x, ytext, fmt.format(s["median"]),
                ha="center", va="bottom", fontsize=fontsize)
        new_top = max(new_top, ytext + pad)
    # Expand y-limit so labels aren't clipped at the top
    if new_top > ymax:
        ax.set_ylim(ymin, new_top)


def format_ax(ax, xlabel, ylabel, x_vals=None, x_sci=False, percent_y=False,
              log_y=False):
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, **GRID_KW)
    if x_vals is not None:
        ax.set_xticks(x_vals)
    if x_sci:
        ax.xaxis.set_major_formatter(ticker.ScalarFormatter(useMathText=True))
        ax.ticklabel_format(axis="x", style="sci", scilimits=(0, 0))
    if percent_y:
        ax.yaxis.set_major_formatter(ticker.PercentFormatter(1.0))
    if log_y:
        ax.set_yscale("log")


def save_fig(fig, outdir, name):
    for fmt in OUTPUT_FMTS:
        p = os.path.join(outdir, f"{name}.{fmt}")
        fig.savefig(p, bbox_inches="tight", dpi=200 if fmt == "png" else None)
    plt.close(fig)
    print(f"  saved {name}")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-B1: Low-contention scale (1k/2k, E1/E2/E3)
# ═════════════════════════════════════════════════════════════════════════════

def plot_B1(groups, outdir):
    nodes = [1000, 2000]
    baselines = ["E1", "E2", "E3"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIG_DUAL)

    # (a) Throughput
    for b in baselines:
        stats = [agg(groups, f"B1-{n}n-{b}", "throughput_pods_per_s") for n in nodes]
        plot_line_iqr(ax1, nodes, stats, COLOR[b], LABEL[b], MARKER[b], LINESTYLE[b])
    format_ax(ax1, "Cluster size (nodes)", "Throughput (pods/s)", nodes, x_sci=True)
    ax1.set_title("(a) Throughput", fontsize=10)
    ax1.legend(fontsize=8, loc="best")

    # (b) ACF (E2/E3 only — E1 has no contention concept at K=0 single-sched)
    for b in ["E2", "E3"]:
        stats = [agg(groups, f"B1-{n}n-{b}", "acf_rate") for n in nodes]
        plot_line_iqr(ax2, nodes, stats, COLOR[b], LABEL[b], MARKER[b], LINESTYLE[b])
    format_ax(ax2, "Cluster size (nodes)", "ACF rate", nodes, x_sci=True)
    ax2.yaxis.set_major_formatter(ticker.PercentFormatter(1.0, decimals=2))
    ax2.set_title("(b) ACF rate", fontsize=10)
    ax2.legend(fontsize=8, loc="best")

    plt.tight_layout()
    save_fig(fig, outdir, "Fig-B1")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-B2: HC-V scale (2k/10k/20k, six baselines + ACF subset)
# ═════════════════════════════════════════════════════════════════════════════

B2_NODES = [2000, 10000, 20000]


def plot_B2_throughput(groups, outdir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    for b in ["E1", "E2", "E3", "P1", "P3", "P4"]:
        stats = [agg(groups, f"B2-{n}n-{b}", "throughput_pods_per_s") for n in B2_NODES]
        plot_line_iqr(ax, B2_NODES, stats, COLOR[b], LABEL[b], MARKER[b], LINESTYLE[b])
    format_ax(ax, "Cluster size (nodes)", "Throughput (pods/s)",
              B2_NODES, x_sci=True, log_y=True)
    ax.set_title("HC-V V=0.6, 10 schedulers", fontsize=10)
    ax.legend(fontsize=7, loc="best", ncol=2)
    plt.tight_layout()
    save_fig(fig, outdir, "Fig-B2a-throughput")


def plot_B2_acf(groups, outdir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    series = ["E2", "E3", "P3", "P4"]
    for b in series:
        stats = [agg(groups, f"B2-{n}n-{b}", "acf_rate") for n in B2_NODES]
        plot_line_iqr(ax, B2_NODES, stats, COLOR[b], LABEL[b], MARKER[b], LINESTYLE[b])
    format_ax(ax, "Cluster size (nodes)", "ACF rate",
              B2_NODES, x_sci=True, percent_y=True)
    ax.set_title("HC-V V=0.6 — vanilla collapses, proposed contains it", fontsize=9)
    ax.legend(fontsize=7, loc="center left")

    # Annotate the "P3 collapse" anchor at 10k nodes (placed top-right, away from legend)
    p3_stat = agg(groups, "B2-10000n-P3", "acf_rate")
    if p3_stat["median"] is not None:
        ax.annotate(f"P3@10k: {p3_stat['median']*100:.1f}%",
                    xy=(10000, p3_stat["median"]),
                    xytext=(13500, p3_stat["median"] - 0.12),
                    fontsize=8, color=COLOR["P3"], fontweight="bold",
                    arrowprops=dict(arrowstyle="->", lw=0.8, color=COLOR["P3"]))

    plt.tight_layout()
    save_fig(fig, outdir, "Fig-B2b-acf")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-B3: scheduler-count scaling
# ═════════════════════════════════════════════════════════════════════════════

B3_N = [2, 4, 6, 8, 10]


def plot_B3_throughput(groups, outdir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    for b in ["E2", "E3", "P1", "P4"]:
        stats = [agg(groups, f"B3-N{n}-{b}", "throughput_pods_per_s") for n in B3_N]
        plot_line_iqr(ax, B3_N, stats, COLOR[b], LABEL[b], MARKER[b], LINESTYLE[b])
    format_ax(ax, "Number of schedulers (N)", "Throughput (pods/s)", B3_N)
    ax.set_title("HC-V 10000 nodes — throughput vs N", fontsize=9)
    ax.legend(fontsize=8, loc="best")
    plt.tight_layout()
    save_fig(fig, outdir, "Fig-B3a-throughput")


def plot_B3_acf(groups, outdir):
    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    for b in ["E2", "E3", "P1", "P4"]:
        stats = [agg(groups, f"B3-N{n}-{b}", "acf_rate") for n in B3_N]
        plot_line_iqr(ax, B3_N, stats, COLOR[b], LABEL[b], MARKER[b], LINESTYLE[b])
    format_ax(ax, "Number of schedulers (N)", "ACF rate", B3_N, percent_y=True)
    ax.set_title("ACF grows with N for vanilla; proposed stays flat", fontsize=9)
    ax.legend(fontsize=8, loc="best")
    plt.tight_layout()
    save_fig(fig, outdir, "Fig-B3b-acf")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-C-Event / Fig-C-Periodic: Ablation
# ═════════════════════════════════════════════════════════════════════════════

def _plot_ablation(groups, outdir, prefix, fig_name, title_suffix):
    """prefix: 'AbE' or 'AbP'."""
    suffixes = ["0-base", "1-M", "2-P", "3-MP"]
    labels = ["Baseline", "+M", "+P", "+M+P (full)"]
    colors = [COLOR["Ab0"], COLOR["Ab1"], COLOR["Ab2"], COLOR["Ab3"]]
    names = [f"{prefix}{s}" for s in suffixes]
    x = np.arange(len(names))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIG_DUAL)

    # (a) ACF
    stats_acf = [agg(groups, n, "acf_rate") for n in names]
    for xi, st, c in zip(x, stats_acf, colors):
        plot_bars_iqr(ax1, [xi], [st], color=c, width=0.7)
    annotate_bars(ax1, x, stats_acf, fmt="{:.1%}", offset_frac=0.025, fontsize=7)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=8)
    ax1.set_ylabel("ACF rate")
    ax1.yaxis.set_major_formatter(ticker.PercentFormatter(1.0))
    ax1.grid(True, axis="y", **GRID_KW)
    ax1.set_title(f"(a) ACF rate — {title_suffix}", fontsize=10)

    # (b) Throughput
    stats_thr = [agg(groups, n, "throughput_pods_per_s") for n in names]
    for xi, st, c in zip(x, stats_thr, colors):
        plot_bars_iqr(ax2, [xi], [st], color=c, width=0.7)
    annotate_bars(ax2, x, stats_thr, fmt="{:.0f}", offset_frac=0.025, fontsize=7)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=8)
    ax2.set_ylabel("Throughput (pods/s)")
    ax2.grid(True, axis="y", **GRID_KW)
    ax2.set_title(f"(b) Throughput — {title_suffix}", fontsize=10)

    plt.tight_layout()
    save_fig(fig, outdir, fig_name)


def plot_C_event(groups, outdir):
    _plot_ablation(groups, outdir, "AbE", "Fig-C-Event", "Event-driven")


def plot_C_periodic(groups, outdir):
    _plot_ablation(groups, outdir, "AbP", "Fig-C-Periodic", "Periodic")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-P: ParSync paper strategy reproduction
# ═════════════════════════════════════════════════════════════════════════════

def plot_P_parsync(groups, outdir):
    """5 strategies × 3 sync_pattern, periodic only (event meaningless for sync_pattern)."""
    strategies = [
        ("QualityFirst",       "QF",    "QF (ours)"),
        ("WeightedRandom",     "WR",    "WR (ours)"),
        ("LatencyFirst",       "LF",    "LF (ours)"),
        ("QualityFirstParSync", "QF-PS", "QF-ParSync (paper)"),
        ("LatencyFirstParSync", "LF-PS", "LF-ParSync (paper)"),
    ]
    sync_patterns = ["glob", "same", "diff"]
    pattern_hatch = {"glob": "", "same": "//", "diff": "xx"}

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIG_DUAL)

    n_strat = len(strategies)
    n_sync = len(sync_patterns)
    width = 0.8 / n_sync
    x = np.arange(n_strat)

    # Track per-pattern handles for legend
    legend_handles = {}

    for j, sp in enumerate(sync_patterns):
        offset = -0.4 + width * (j + 0.5)
        acf_stats, thr_stats, colors = [], [], []
        for strat_full, key, _label in strategies:
            name = f"S-P-Strategy-{strat_full}-{sp}"
            acf_stats.append(agg(groups, name, "acf_rate"))
            thr_stats.append(agg(groups, name, "throughput_pods_per_s"))
            colors.append(COLOR[key])
        for i in range(n_strat):
            bar_acf, _, _ = plot_bars_iqr(ax1, [x[i] + offset], [acf_stats[i]],
                                          color=colors[i], hatch=pattern_hatch[sp],
                                          width=width)
            bar_thr, _, _ = plot_bars_iqr(ax2, [x[i] + offset], [thr_stats[i]],
                                          color=colors[i], hatch=pattern_hatch[sp],
                                          width=width)
        # legend proxy: gray bar with the pattern's hatch
        if sp not in legend_handles:
            legend_handles[sp] = ax1.bar([np.nan], [np.nan], color="lightgray",
                                         hatch=pattern_hatch[sp],
                                         edgecolor="black", label=f"sync={sp}")

    strat_labels = [s[2] for s in strategies]
    for ax in (ax1, ax2):
        ax.set_xticks(x)
        ax.set_xticklabels(strat_labels, rotation=20, ha="right", fontsize=7)
        ax.grid(True, axis="y", **GRID_KW)
    ax1.set_ylabel("ACF rate")
    ax1.yaxis.set_major_formatter(ticker.PercentFormatter(1.0))
    ax1.set_title("(a) ACF rate — paper QF mode failure reproduced", fontsize=9)
    ax2.set_ylabel("Throughput (pods/s)")
    ax2.set_title("(b) Throughput", fontsize=10)

    ax1.legend(handles=list(legend_handles.values()), fontsize=7, loc="best")

    plt.tight_layout()
    save_fig(fig, outdir, "Fig-P-parsync")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-Paradigm-Cost: vanilla vs proposed cost, both paradigms (B2-10000n)
# ═════════════════════════════════════════════════════════════════════════════

def plot_paradigm_cost(groups, outdir):
    """Compare E2/E3/P1/P4 at 10000n on 4 metrics — proposed vs vanilla within each paradigm."""
    bls = ["E2", "E3", "P1", "P4"]
    names = [f"B2-10000n-{b}" for b in bls]
    colors = [COLOR[b] for b in bls]
    labels = [LABEL[b] for b in bls]
    x = np.arange(len(bls))

    fig, axes = plt.subplots(1, 4, figsize=FIG_QUAD)

    metrics = [
        ("throughput_pods_per_s", "Throughput (pods/s)", "(a) Throughput", None,    "{:.0f}"),
        ("acf_rate",              "ACF rate",            "(b) ACF rate",   "pct",   "{:.1%}"),
        ("algo_p99_ms",           "Algo P99 (ms)",       "(c) Algo P99",   None,    "{:.0f}"),
        ("scheduler_cpu_total",   "Scheduler CPU (cores·s)",  "(d) Scheduler CPU", None, "{:.1f}"),
    ]

    for ax, (field, ylabel, title, fmt_kind, num_fmt) in zip(axes, metrics):
        filt = algo_filter if field == "algo_p99_ms" else None
        stats = [agg(groups, n, field, filt) for n in names]
        for xi, st, c in zip(x, stats, colors):
            plot_bars_iqr(ax, [xi], [st], color=c, width=0.7)
        annotate_bars(ax, x, stats, fmt=num_fmt, offset_frac=0.03, fontsize=7)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=7)
        ax.set_ylabel(ylabel)
        ax.grid(True, axis="y", **GRID_KW)
        ax.set_title(title, fontsize=10)
        if fmt_kind == "pct":
            ax.yaxis.set_major_formatter(ticker.PercentFormatter(1.0))

    fig.suptitle("Paradigm cost: vanilla vs proposed (B2-10000n, V=0.6, N=10)",
                 fontsize=10, y=1.02)
    plt.tight_layout()
    save_fig(fig, outdir, "Fig-Paradigm-Cost")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-D1: Application-level (D-new, 5-worker physical cluster)
# ═════════════════════════════════════════════════════════════════════════════

def plot_D1(groups, outdir):
    """D1 application-level performance × strategy × stress profile.

    Data: 4 strategies (E2/E3/P1/P4) × 3 profiles (none/mild/heavy) × 5 trials.
    See analysis-report §9.1.4 — D-new corrects v6 §9.1 trial-order drift.
    Key finding: P1 (periodic vanilla) outperforms E2 in nginx by 17–24%.
    """
    profiles = ["none", "mild", "heavy"]
    strategies = ["E2", "E3", "P1", "P4"]

    metrics = [
        ("nginx_qps",        "nginx QPS",       "(a) nginx QPS",       "{:.0f}"),
        ("nginx_lat_p99_ms", "nginx P99 (ms)",  "(b) nginx P99",       "{:.1f}"),
        ("redis_set_ops",    "redis SET ops/s", "(c) redis SET",       "{:.0f}"),
        ("mysql_tps",        "mysql TPS",       "(d) mysql TPS",       "{:.1f}"),
    ]

    fig, axes = plt.subplots(1, 4, figsize=(14.0, 3.8), constrained_layout=True)

    n_strats = len(strategies)
    width = 0.8 / n_strats
    x = np.arange(len(profiles))

    for ax, (field, ylabel, title, num_fmt) in zip(axes, metrics):
        for i, strat in enumerate(strategies):
            offset = -0.4 + width * (i + 0.5)
            stats = [agg(groups, f"D1-{strat}-{p}", field) for p in profiles]
            xpos = [xj + offset for xj in x]
            for j, st in enumerate(stats):
                plot_bars_iqr(ax, [xpos[j]], [st],
                              color=COLOR[strat], width=width)
            # Annotate medians (small font, above error caps)
            annotate_bars(ax, xpos, stats, fmt=num_fmt,
                          offset_frac=0.025, fontsize=6)

        ax.set_xticks(x)
        ax.set_xticklabels(profiles, fontsize=8)
        ax.set_xlabel("Stress profile")
        ax.set_ylabel(ylabel)
        ax.grid(True, axis="y", **GRID_KW)
        ax.set_title(title, fontsize=10)

    # Title above; shared legend just below title, above subplots
    fig.suptitle("D1 application layer — 5-worker physical cluster (D-new, n=5/cell)",
                 fontsize=11, y=1.04)
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLOR[s]) for s in strategies]
    labels = [f"{s} — {LABEL[s]}" for s in strategies]
    fig.legend(handles, labels, loc="upper center", ncol=4, fontsize=9,
               bbox_to_anchor=(0.5, 0.99), frameon=False)

    save_fig(fig, outdir, "Fig-D1")


# ═════════════════════════════════════════════════════════════════════════════
#  Fig-Sens-K / Fig-Sens-p: parameter sensitivity (Board A)
# ═════════════════════════════════════════════════════════════════════════════

def _dual_axis_sens(ax, x_vals, x_label, acf_stats, thr_stats, color_acf, color_thr):
    """Shared dual-axis renderer for sensitivity plots."""
    med_a, lo_a, hi_a = _line_arrays(acf_stats)
    med_t, lo_t, hi_t = _line_arrays(thr_stats)

    xa = np.array(x_vals, dtype=float)

    # Primary: ACF
    mask_a = np.isfinite(med_a)
    ax.plot(xa[mask_a], med_a[mask_a], color=color_acf, marker="o",
            ls="-", lw=1.6, label="ACF rate")
    ax.fill_between(xa[mask_a], (med_a - lo_a)[mask_a], (med_a + hi_a)[mask_a],
                    color=color_acf, alpha=FILL_ALPHA)
    ax.set_xlabel(x_label)
    ax.set_ylabel("ACF rate", color=color_acf)
    ax.tick_params(axis="y", labelcolor=color_acf)
    ax.yaxis.set_major_formatter(ticker.PercentFormatter(1.0))
    ax.set_xticks(x_vals)
    ax.grid(True, **GRID_KW)

    # Secondary: throughput
    ax2 = ax.twinx()
    mask_t = np.isfinite(med_t)
    ax2.plot(xa[mask_t], med_t[mask_t], color=color_thr, marker="s",
             ls="--", lw=1.6, label="Throughput")
    ax2.fill_between(xa[mask_t], (med_t - lo_t)[mask_t], (med_t + hi_t)[mask_t],
                     color=color_thr, alpha=FILL_ALPHA)
    ax2.set_ylabel("Throughput (pods/s)", color=color_thr)
    ax2.tick_params(axis="y", labelcolor=color_thr)

    # Combined legend
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="center right")


def plot_sens_K(groups, outdir):
    """K sensitivity: 1×2 (Event / Periodic), dual-axis ACF + throughput."""
    ks = [0, 1, 2, 4]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.0, 3.4),
                                   constrained_layout=True)

    for ax, prefix, title in [
        (ax1, "S-E-K", "(a) Event-driven: K sensitivity"),
        (ax2, "S-P-K", "(b) Periodic: K sensitivity"),
    ]:
        acf_stats = [agg(groups, f"{prefix}{k}", "acf_rate") for k in ks]
        thr_stats = [agg(groups, f"{prefix}{k}", "throughput_pods_per_s") for k in ks]
        _dual_axis_sens(ax, ks, "candidate_k (K)", acf_stats, thr_stats,
                        COLOR["E3"], COLOR["E2"])
        ax.set_title(title, fontsize=10)

    save_fig(fig, outdir, "Fig-Sens-K")


def plot_sens_p(groups, outdir):
    """penalty_weight sensitivity: 1×2 (Event diff / Periodic glob)."""
    ps = [0.0, 0.1, 0.3, 0.5, 0.7]
    p_codes = ["00", "01", "03", "05", "07"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.0, 3.4),
                                   constrained_layout=True)

    # Event: only diff makes sense
    acf_e = [agg(groups, f"S-E-P{c}-diff", "acf_rate") for c in p_codes]
    thr_e = [agg(groups, f"S-E-P{c}-diff", "throughput_pods_per_s") for c in p_codes]
    _dual_axis_sens(ax1, ps, "penalty_weight (p)", acf_e, thr_e,
                    COLOR["E3"], COLOR["E2"])
    ax1.set_title("(a) Event-driven (sync=diff)", fontsize=10)

    # Periodic: glob is the recommended sync_pattern
    acf_p = [agg(groups, f"S-P-P{c}-glob", "acf_rate") for c in p_codes]
    thr_p = [agg(groups, f"S-P-P{c}-glob", "throughput_pods_per_s") for c in p_codes]
    _dual_axis_sens(ax2, ps, "penalty_weight (p)", acf_p, thr_p,
                    COLOR["E3"], COLOR["E2"])
    ax2.set_title("(b) Periodic (sync=glob)", fontsize=10)

    save_fig(fig, outdir, "Fig-Sens-p")


# ═════════════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════════════

PLOTTERS = [
    ("Fig-B1",             plot_B1),
    ("Fig-B2a-throughput", plot_B2_throughput),
    ("Fig-B2b-acf",        plot_B2_acf),
    ("Fig-B3a-throughput", plot_B3_throughput),
    ("Fig-B3b-acf",        plot_B3_acf),
    ("Fig-C-Event",        plot_C_event),
    ("Fig-C-Periodic",     plot_C_periodic),
    ("Fig-P-parsync",      plot_P_parsync),
    ("Fig-Paradigm-Cost",  plot_paradigm_cost),
    ("Fig-D1",             plot_D1),
    ("Fig-Sens-K",         plot_sens_K),
    ("Fig-Sens-p",         plot_sens_p),
]


def main():
    ap = argparse.ArgumentParser(description="Generate paper figures from cluster results")
    ap.add_argument("--results-root", default="experiments/results",
                    help="Root containing per-board summary.json files")
    ap.add_argument("--outdir", default="experiments/results/figures",
                    help="Output directory for figures")
    ap.add_argument("--only", nargs="*", default=None,
                    help="Only render these figure names (substring match)")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    print(f"Loading from {args.results_root}/")
    records = load_all(args.results_root)
    groups = group_records(records)
    print(f"Total {len(records)} trials → {len(groups)} experiments after filtering")
    print(f"  excluded {len(SKIP_EXPERIMENTS)} known-bad experiment names")

    todo = PLOTTERS
    if args.only:
        todo = [(n, fn) for n, fn in PLOTTERS
                if any(s.lower() in n.lower() for s in args.only)]

    print(f"\nRendering {len(todo)} figures → {args.outdir}/")
    for name, fn in todo:
        try:
            fn(groups, args.outdir)
        except Exception as e:
            print(f"  SKIP {name}: {type(e).__name__}: {e}")

    print(f"\nDone.")


if __name__ == "__main__":
    main()
