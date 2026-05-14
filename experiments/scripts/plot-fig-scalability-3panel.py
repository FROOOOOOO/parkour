#!/usr/bin/env python3
"""
Paper Figure: Scalability summary.

Layout: 1 row x 3 columns at width ratio 2:3:3.
  (a) B1 low-contention throughput vs cluster size       width=2
  (b) B3 scheduler-count throughput                      width=3
  (c) B3 scheduler-count ACF                             width=3

The scheduler-count sweep is split into separate throughput and ACF panels
instead of using a dual y-axis. Output is paper/figs/eval-main.{pdf,svg}.

Usage:
    python plot-fig-eval-main.py
    python plot-fig-eval-main.py --show
"""

import argparse
import csv
import os
import warnings
from collections import defaultdict

import matplotlib.lines as mlines
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import PercentFormatter

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
FIG_DIR = os.path.join(_REPO, "paper", "figs")
B1_CSV_PATH = os.path.join(_REPO, "experiments", "results", "B1", "summary.csv")

# ---------------------------------------------------------------------------
#  Colours (aligned with plot-fig-scalability.py)
# ---------------------------------------------------------------------------
_rdbu = sns.color_palette("RdBu", 11)

# Scalability palette (B1, B3): grey + red/green light/dark.
C_GREY = "#999999"
C_RED_D = "#{:02x}{:02x}{:02x}".format(
    int(_rdbu[1][0] * 255),
    int(_rdbu[1][1] * 255),
    int(_rdbu[1][2] * 255),
)
C_RED_L = "#{:02x}{:02x}{:02x}".format(
    int(_rdbu[3][0] * 255),
    int(_rdbu[3][1] * 255),
    int(_rdbu[3][2] * 255),
)
C_GREEN_D = "#238b45"
C_GREEN_L = "#74c476"
C_PURPLE  = "#A78AB8"  # Godel baseline — light purple, static-partitioning

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size": 9,
    "axes.labelsize": 8.5,
    "axes.titlesize": 8.5,
    "xtick.labelsize": 7.5,
    "ytick.labelsize": 7.5,
    "legend.fontsize": 7,
    "axes.linewidth": 0.8,
    "lines.linewidth": 1.4,
    "lines.markersize": 2.5,
}

LW = 1.4
ALPHA_BAND = 0.12

# ---------------------------------------------------------------------------
#  Data - eval-data.md section 1.2 / 1.3
# ---------------------------------------------------------------------------

# B1: low-contention scaling
B1_X = [1000, 2000, 5000]
B1_X_LABELS = ["1K", "2K", "5K"]
B1_EXP = {
    "single": ["B1-1000n-E1", "B1-2000n-E1", "B1-5000n-E1"],
    "vanilla-E": ["B1-1000n-E2", "B1-2000n-E2", "B1-5000n-E2"],
    "ParKour-E": ["B1-1000n-E3", "B1-2000n-E3", "B1-5000n-E3"],
}

# B3: scheduler-count scaling
B3_X = [2, 4, 6, 8, 10]
B3_E2_TPUT = [52.2, 105.3, 150.9, 202.1, 247.9]
B3_E2_ACF = [0.0079, 0.0206, 0.0348, 0.0456, 0.0536]
B3_E3_TPUT = [51.3, 106.3, 159.0, 210.8, 260.8]
B3_E3_ACF = [0.0001, 0.0021, 0.0048, 0.0072, 0.0137]
B3_P1_TPUT = [34.7, 47.6, 52.2, 87.6, 92.6]
B3_P1_ACF = [0.2370, 0.4875, 0.5854, 0.4451, 0.4697]
B3_P4_TPUT = [48.4, 98.4, 133.8, 160.5, 212.3]
B3_P4_ACF = [0.0937, 0.1156, 0.1462, 0.1623, 0.1134]

# Godel baseline — precomputed offline from experiments/results/godel-new/
# via snap_summary.json after backfill (saturation-window divisor + all-reason
# failure counter). Same outlier filter (dur > max(Q3+1.5*IQR, 2*median)) as
# the para-scheduler trial filter; N(N=2)=4 due to one dropped slow trial.
# Godel uses a different metric pipeline so it is not loaded via B1_CSV_PATH.
B1_GODEL_STATS = {
    "median": np.array([224.6, 209.0, 131.1]),
    "q1":     np.array([224.4, 205.6, 131.1]),
    "q3":     np.array([228.4, 214.5, 131.2]),
}
B3_GODEL_TPUT = [213.2, 139.2, 208.7, 213.3, 161.7]
B3_GODEL_ACF  = [0.0021, 0.0026, 0.0018, 0.0023, 0.0024]


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def _f(value):
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _filter_trials(trials):
    valid = []
    for trial in trials:
        scheduled = _f(trial.get("scheduled_pods"))
        if scheduled is not None and scheduled > 0:
            valid.append(trial)
    if not valid:
        return []

    bind_rates = [_f(t.get("bind_conflict_rate")) or 0.0 for t in valid]
    cell_has_conflict = sum(1 for rate in bind_rates if rate > 0.01) >= 2
    no_hole = []
    for trial in valid:
        scheduled = _f(trial.get("scheduled_pods"))
        expected = _f(trial.get("expected_pods")) or scheduled
        acf_count = _f(trial.get("acf_count"))
        bind_count = _f(trial.get("bind_conflict_count"))
        is_hole = (
            cell_has_conflict
            and acf_count == 0
            and bind_count == 0
            and expected
            and scheduled / expected >= 0.9
        )
        if not is_hole:
            no_hole.append(trial)

    if len(no_hole) < 4:
        return no_hole

    durations = np.array([_f(t.get("scheduling_duration_s")) for t in no_hole])
    if np.sum(np.isfinite(durations)) < 4:
        return no_hole
    q1, q3 = np.percentile(durations, [25, 75])
    median = np.median(durations)
    upper = max(q3 + 1.5 * (q3 - q1), 2.0 * median)
    return [
        trial for trial, duration in zip(no_hole, durations)
        if np.isfinite(duration) and duration <= upper
    ]


def _b1_stats():
    by_exp = defaultdict(list)
    with open(B1_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_exp[row.get("experiment", "")].append(row)

    out = {}
    for label, experiments in B1_EXP.items():
        medians, q1s, q3s = [], [], []
        for exp in experiments:
            kept = _filter_trials(by_exp[exp])
            values = np.array([
                _f(t.get("throughput_pods_per_s")) for t in kept
                if _f(t.get("throughput_pods_per_s")) is not None
            ])
            if len(values) == 0:
                medians.append(np.nan)
                q1s.append(np.nan)
                q3s.append(np.nan)
                continue
            q1, median, q3 = np.percentile(values, [25, 50, 75])
            medians.append(median)
            q1s.append(q1)
            q3s.append(q3)
        out[label] = {
            "median": np.array(medians),
            "q1": np.array(q1s),
            "q3": np.array(q3s),
        }
    return out


def _draw_iqr_line(ax, x, stats, color, marker, markerfacecolor=None):
    median = stats["median"]
    q1 = stats["q1"]
    q3 = stats["q3"]
    kw = dict(color=color, linestyle="-", linewidth=LW,
              marker=marker, markersize=3.5)
    if markerfacecolor is not None:
        kw["markerfacecolor"] = markerfacecolor
    ax.plot(x, median, **kw)
    ax.fill_between(
        x,
        np.maximum(0, q1),
        q3,
        color=color,
        alpha=ALPHA_BAND,
        linewidth=0,
    )


def _draw_b1(ax, stats):
    xpos = np.array(B1_X, dtype=float)

    series = [
        ("single", C_GREY, "D", "white"),
        ("vanilla-E", C_RED_D, "o", "white"),
        ("ParKour-E", C_GREEN_D, "o", None),
    ]
    for label, color, marker, markerfacecolor in series:
        _draw_iqr_line(ax, xpos, stats[label], color, marker, markerfacecolor)

    # Godel baseline — hardcoded stats (different metric pipeline, see header).
    _draw_iqr_line(ax, xpos, B1_GODEL_STATS, C_PURPLE, "^", "white")

    ax.set_xticks(xpos)
    ax.set_xticklabels(B1_X_LABELS)
    ax.set_title("(a)")
    ax.set_xlabel("Cluster size (nodes)")
    ax.set_ylabel("TP (pods/s)")
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)


def _draw_b3_tput(ax):
    x = np.array(B3_X, dtype=float)

    # Shaded gap: ParKour-P advantage over vanilla-P in throughput.
    ax.fill_between(x, B3_P1_TPUT, B3_P4_TPUT,
                    color=C_GREEN_L, alpha=0.18, linewidth=0, zorder=1)

    ax.plot(x, B3_E2_TPUT, color=C_RED_D, linestyle="-", linewidth=LW,
            marker="o", markersize=3.5, markerfacecolor="white", zorder=3)
    ax.plot(x, B3_E3_TPUT, color=C_GREEN_D, linestyle="-", linewidth=LW,
            marker="o", markersize=3.5, zorder=3)
    ax.plot(x, B3_P1_TPUT, color=C_RED_L, linestyle="-", linewidth=LW,
            marker="s", markersize=3.5, markerfacecolor="white", zorder=3)
    ax.plot(x, B3_P4_TPUT, color=C_GREEN_L, linestyle="-", linewidth=LW,
            marker="s", markersize=3.5, zorder=3)
    ax.plot(x, B3_GODEL_TPUT, color=C_PURPLE, linestyle="-", linewidth=LW,
            marker="^", markersize=3.5, markerfacecolor="white", zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels([str(v) for v in B3_X])
    ax.set_title("(b)")
    ax.set_xlabel("Number of schedulers")
    ax.set_ylabel("TP (pods/s)")
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)


def _draw_b3_acf(ax):
    x = np.array(B3_X, dtype=float)

    # Horizontal band: mark ParKour-P's stable operating range [min, max].
    # Vanilla-P fluctuates wildly (24–58%) while ParKour-P stays within
    # this bounded corridor regardless of N — that is the message.
    p4_lo, p4_hi = min(B3_P4_ACF), max(B3_P4_ACF)
    ax.axhspan(p4_lo, p4_hi, color=C_GREEN_L, alpha=0.20, linewidth=0, zorder=1)

    ax.plot(x, B3_E2_ACF, color=C_RED_D, linestyle="-", linewidth=LW,
            marker="o", markersize=3.5, markerfacecolor="white", zorder=3)
    ax.plot(x, B3_E3_ACF, color=C_GREEN_D, linestyle="-", linewidth=LW,
            marker="o", markersize=3.5, zorder=3)
    ax.plot(x, B3_P1_ACF, color=C_RED_L, linestyle="-", linewidth=LW,
            marker="s", markersize=3.5, markerfacecolor="white", zorder=3)
    ax.plot(x, B3_P4_ACF, color=C_GREEN_L, linestyle="-", linewidth=LW,
            marker="s", markersize=3.5, zorder=3)
    ax.plot(x, B3_GODEL_ACF, color=C_PURPLE, linestyle="-", linewidth=LW,
            marker="^", markersize=3.5, markerfacecolor="white", zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels([str(v) for v in B3_X])
    ax.set_title("(c)")
    ax.set_xlabel("Number of schedulers")
    ax.set_ylabel("ACF rate")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_ylim(-0.02, 0.66)
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)


# ---------------------------------------------------------------------------
#  Build figure
# ---------------------------------------------------------------------------

def build_figure():
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)
    b1 = _b1_stats()

    fig = plt.figure(figsize=(7.0, 2.0))
    gs = GridSpec(
        1,
        3,
        figure=fig,
        width_ratios=[2, 3, 3],
        wspace=0.42,
        left=0.06,
        right=0.985,
        top=0.82,
        bottom=0.24,
    )

    ax_b1 = fig.add_subplot(gs[0, 0])
    ax_tput = fig.add_subplot(gs[0, 1])
    ax_acf = fig.add_subplot(gs[0, 2])

    _draw_b1(ax_b1, b1)
    _draw_b3_tput(ax_tput)
    _draw_b3_acf(ax_acf)

    h_single = mlines.Line2D(
        [], [], color=C_GREY, marker="D", markersize=3.5,
        markerfacecolor="white", linewidth=LW, label="single",
    )
    h_v_e = mlines.Line2D(
        [], [], color=C_RED_D, marker="o", markersize=3.5,
        markerfacecolor="white", linewidth=LW, label="vanilla-E",
    )
    h_pk_e = mlines.Line2D(
        [], [], color=C_GREEN_D, marker="o", markersize=3.5,
        linewidth=LW, label="ParKour-E",
    )
    h_v_p = mlines.Line2D(
        [], [], color=C_RED_L, marker="s", markersize=3.5,
        markerfacecolor="white", linewidth=LW, label="vanilla-P",
    )
    h_pk_p = mlines.Line2D(
        [], [], color=C_GREEN_L, marker="s", markersize=3.5,
        linewidth=LW, label="ParKour-P",
    )
    h_godel = mlines.Line2D(
        [], [], color=C_PURPLE, marker="^", markersize=3.5,
        markerfacecolor="white", linewidth=LW, label="Godel",
    )

    fig.legend(
        handles=[h_single, h_v_e, h_pk_e, h_v_p, h_pk_p, h_godel],
        loc=(0.22, 0.9),
        ncol=6,
        frameon=False,
        fontsize=7,
        columnspacing=0.8,
        handlelength=1.3,
        handletextpad=0.35,
    )

    return fig


def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"eval-main.{ext}")
        fig.savefig(p, bbox_inches="tight", pad_inches=0)
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure - scalability summary (1x3, 2:3:3)"
    )
    ap.add_argument("--show", action="store_true",
                    help="Display interactively after saving")
    args = ap.parse_args()

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
