#!/usr/bin/env python3
"""
Plot Pareto scatter: throughput vs ACF rate for B2 experiments.

Four points per strategy (2k/5k/10k/20k), connected by a scaling trajectory.

Data: experiments/results/B2/summary.csv

Run from the repository root:
    conda activate para-sched
    python experiments/scripts/plot-pareto.py
"""

import csv
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as ticker
from matplotlib.legend import Legend
import numpy as np
import seaborn as sns

# ── global style ──────────────────────────────────────────────────────────────
sns.set_style("ticks")
sns.set_context("paper", font_scale=1.3)
plt.rcParams["font.family"]     = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Arial", "Helvetica", "DejaVu Sans"]
plt.rcParams["axes.labelsize"]  = 12
plt.rcParams["axes.titlesize"]  = 12
plt.rcParams["xtick.labelsize"] = 11
plt.rcParams["ytick.labelsize"] = 11
plt.rcParams["legend.fontsize"] = 10.5
plt.rcParams["legend.title_fontsize"] = 11
# fonttype 42 keeps the PDF/PS text as embedded TrueType subsets. Matplotlib
# defaults to Type 3, which ACM camera-ready rejects and which also leaves the
# figure text unsearchable.
plt.rcParams["pdf.fonttype"]    = 42
plt.rcParams["ps.fonttype"]     = 42
plt.rcParams["svg.fonttype"]    = "none"
# Mathtext carries its own font set on top of font.family, so it has to be
# pointed at Arial too or any $...$ label renders in matplotlib's DejaVu.
plt.rcParams["mathtext.fontset"] = "custom"
plt.rcParams["mathtext.rm"]      = "Arial"
plt.rcParams["mathtext.it"]      = "Arial:italic"
plt.rcParams["mathtext.bf"]      = "Arial:bold"

GRID_KW      = dict(linestyle="--", alpha=0.45, linewidth=0.8)
OUTPUT_FMTS  = ("pdf", "svg", "png")

# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.join(SCRIPT_DIR, "..", "..")
CSV_PATH    = os.path.join(REPO_ROOT, "experiments", "results", "B2", "summary.csv")
OUT_DIR     = os.path.join(REPO_ROOT, "paper", "figs")
os.makedirs(OUT_DIR, exist_ok=True)

# ── visual style ───────────────────────────────────────────────────────────────
# Color families: E2/P1 red, P2/P3 blue, E3/P4 green
# Event-driven = lighter, periodic = darker
COLOR = {
    "E2": "#F4845F",   # light coral-red  (event-driven)
    "P1": "#A01010",   # deep crimson      (periodic)
    "P2": "#7BAFD4",   # steel blue        (periodic lighter of the pair)
    "P3": "#1A4E8A",   # dark navy         (periodic darker)
    "E3": "#6DBF67",   # light sage-green  (event-driven)
    "P4": "#1A6B2A",   # deep forest-green (periodic)
    "G":  "#A78AB8",   # light purple      (Godel baseline)
}

MARKER = {
    "E2": "o",    # circle
    "P1": "s",    # square
    "P2": "^",    # triangle up
    "P3": "v",    # triangle down
    "E3": "D",    # diamond
    "P4": "p",    # pentagon
    "G":  "8",    # (Godel)
}

LABEL = {
    "E2": "Vanilla (event-driven)",
    "P1": "Vanilla (periodic)",
    "P2": "sameSync",
    "P3": "diffSync",
    "E3": "ParKour (event-driven)",
    "P4": "ParKour (periodic)",
    "G":  "Gödel",
}

NODE_SIZES = [2000, 5000, 10000, 20000]
STRATEGIES = ["E2", "P1", "P2", "P3", "E3", "P4", "G"]

# legend group order for visual separation
GROUP_ORDER = [
    ("E2", "P1"),   # red family
    ("P2", "P3"),   # blue family
    ("E3", "P4"),   # green family
]

# ── data loading ───────────────────────────────────────────────────────────────
def load_data():
    """Return dict: (strategy, num_nodes) -> {'throughput', 'acf', 'duration',
    'trial'} (parallel lists; one entry per trial)."""
    raw = defaultdict(lambda: {"throughput": [], "acf": [],
                                "duration": [], "trial": []})

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            exp = row["experiment"]
            parts = exp.split("-")
            if len(parts) < 3:
                continue
            strategy = parts[2]
            if strategy not in STRATEGIES:
                continue

            try:
                nodes = int(parts[1].rstrip("n"))
                tp    = float(row["throughput_pods_per_s"])
                acf   = float(row["acf_rate"])
                dur   = float(row["scheduling_duration_s"])
            except (ValueError, KeyError):
                continue

            # skip invalid trials
            if not row.get("scheduled_pods") or float(row["scheduled_pods"]) <= 0:
                continue

            raw[(strategy, nodes)]["throughput"].append(tp)
            raw[(strategy, nodes)]["acf"].append(acf)
            raw[(strategy, nodes)]["duration"].append(dur)
            raw[(strategy, nodes)]["trial"].append(row.get("trial", "?"))

    return raw


def filter_duration_outliers(raw):
    """Drop trials with anomalously long scheduling_duration_s within their
    (strategy, nodes) cell.

    Outlier criterion (must satisfy BOTH):
      (1) dur > Q3 + 1.5*IQR   — standard Tukey rule
      (2) dur > 2 * median     — magnitude floor; guards against false
                                  positives when IQR is narrow (e.g. all
                                  trials clustered tightly).

    Rationale: end-to-end Pareto shows steady-state behaviour; trials that
    stalled (retry loops, kwok hiccups, cluster warmup) inflate duration
    disproportionately and bias the median toward pathological cases.
    Filtering on duration is preferred over filtering on throughput itself
    because it avoids cherry-picking high-throughput runs.

    Cells with <4 trials are left untouched (IQR statistics unreliable).
    """
    filtered = {}
    drops = []
    for key, vals in raw.items():
        durs = np.array(vals["duration"])
        if len(durs) < 4:
            filtered[key] = vals
            continue
        q1, q3 = np.percentile(durs, [25, 75])
        median = np.median(durs)
        upper = max(q3 + 1.5 * (q3 - q1), 2.0 * median)
        keep = [i for i, d in enumerate(durs) if d <= upper]
        if len(keep) < len(durs):
            for i, d in enumerate(durs):
                if d > upper:
                    drops.append((key, vals["trial"][i], d, upper,
                                  vals["throughput"][i], vals["acf"][i]))
            filtered[key] = {k: [v[i] for i in keep] for k, v in vals.items()}
        else:
            filtered[key] = vals

    if drops:
        print("Dropped duration outliers (> max(Q3+1.5*IQR, 2*median)):")
        for (strat, n), trial, d, upper, tp, acf in drops:
            print(f"  {strat:3s} {n:6d}n  trial={trial}  dur={d:6.1f}s "
                  f"> upper={upper:6.1f}s  (tp={tp:.1f}, acf={acf:.3f})")
    else:
        print("No duration outliers detected.")
    return filtered


def aggregate(raw):
    """Collapse trials -> median ± IQR per (strategy, nodes) point."""
    result = {}
    for key, vals in raw.items():
        tp_arr  = np.array(vals["throughput"])
        acf_arr = np.array(vals["acf"])
        result[key] = {
            "tp_med":  np.median(tp_arr),
            "tp_q1":   np.percentile(tp_arr, 25),
            "tp_q3":   np.percentile(tp_arr, 75),
            "acf_med": np.median(acf_arr),
            "acf_q1":  np.percentile(acf_arr, 25),
            "acf_q3":  np.percentile(acf_arr, 75),
            "n":        len(tp_arr),
        }
    return result


# Godel baseline data — precomputed offline from experiments/results/godel-new/
# via snap_summary.json after backfill-godel-conflicts.sh corrections (saturation-
# window divisor + all-reason failure counter). Same outlier filter as B2 cells.
# Injected directly at the aggregate level rather than via CSV since godel uses
# a different metric pipeline (binder_binding_pod_attempts vs parasched_bind_*).
GODEL_B2 = {
    2000:  dict(tp_med=225.2, tp_q1=225.1, tp_q3=225.8,
                acf_med=0.0128, acf_q1=0.0123, acf_q3=0.0133, n=5),
    5000:  dict(tp_med=228.2, tp_q1=218.9, tp_q3=228.8,
                acf_med=0.0068, acf_q1=0.0044, acf_q3=0.0068, n=5),
    10000: dict(tp_med=213.2, tp_q1=208.8, tp_q3=213.3,
                acf_med=0.0024, acf_q1=0.0021, acf_q3=0.0025, n=5),
    20000: dict(tp_med=150.3, tp_q1=119.2, tp_q3=182.5,
                acf_med=0.0027, acf_q1=0.0022, acf_q3=0.0038, n=4),
}


def inject_godel(data):
    for nodes, d in GODEL_B2.items():
        data[("G", nodes)] = d
    return data


# ── shared plot helpers ─────────────────────────────────────────────────────────
def _setup_axes(ax):
    ax.set_xlabel("ACF rate")
    ax.set_ylabel("Throughput (pods/s)")
    ax.xaxis.set_major_formatter(ticker.PercentFormatter(1.0, decimals=0))
    ax.grid(True, **GRID_KW, zorder=0)


def save_fig(fig, out_dir, name):
    # Include all artists (incl. legends placed via bbox_to_anchor outside the
    # axes) when computing the saved bounding box; otherwise long legend
    # labels can get clipped on the right edge.
    extras = list(fig.findobj(Legend))
    for fmt in OUTPUT_FMTS:
        p = os.path.join(out_dir, f"{name}.{fmt}")
        fig.savefig(p, bbox_inches="tight", pad_inches=0,
                    bbox_extra_artists=extras,
                    dpi=200 if fmt == "png" else None)
    plt.close(fig)
    print(f"  saved {name}")


# ── Pareto across scales: 2k / 5k / 10k / 20k ─────────────────────────────────
# Marker size encodes node count (small=2k, large=20k) so scale is
# legible at a glance without per-point text labels (which collide in dense
# regions like P1/P2/P3 at high ACF).
NODE_MSIZE = {2000: 5, 5000: 8, 10000: 11, 20000: 14}
NODE_LABEL = {
    2000: "2k",
    5000: "5k",
    10000: "10k",
    20000: "20k",
}


def _scale_legend_handles():
    """Gray circle markers of varying size, used as a separate scale legend."""
    return [
        plt.Line2D([0], [0], marker="o", color="#888888",
                   markersize=NODE_MSIZE[n], linewidth=0,
                   markeredgecolor="white", markeredgewidth=0.9,
                   label=NODE_LABEL[n])
        for n in NODE_SIZES
    ]


def _legend_handles(strategies):
    return [
        plt.Line2D([0], [0], marker=MARKER[s], color=COLOR[s],
                   markersize=8, linewidth=0,
                   markeredgecolor="white", markeredgewidth=0.9,
                   label=LABEL[s])
        for s in strategies
    ]


def plot_all_scales(data, out_dir):
    fig, ax = plt.subplots(figsize=(6.5, 4.8))
    _setup_axes(ax)

    for strat in STRATEGIES:
        xs, ys = [], []  # ordered by ascending node count for trajectory line
        for nodes in NODE_SIZES:
            key = (strat, nodes)
            if key not in data:
                xs.append(None); ys.append(None)
                continue
            d = data[key]
            xs.append(d["acf_med"])
            ys.append(d["tp_med"])

        # trajectory line connecting valid points (2K → 20K direction)
        valid = [(x, y) for x, y in zip(xs, ys) if x is not None]
        if len(valid) >= 2:
            vx, vy = zip(*valid)
            ax.plot(vx, vy, color=COLOR[strat], linewidth=1.2,
                    linestyle="--", alpha=0.55, zorder=3)

        # scatter each node-size point with IQR error bars; size encodes scale
        for nodes, x, y in zip(NODE_SIZES, xs, ys):
            if x is None:
                continue
            d = data[(strat, nodes)]
            xerr = np.array([[d["acf_med"] - d["acf_q1"]],
                              [d["acf_q3"] - d["acf_med"]]])
            yerr = np.array([[d["tp_med"]  - d["tp_q1"]],
                              [d["tp_q3"]  - d["tp_med"]]])

            ax.errorbar(
                x, y,
                xerr=xerr, yerr=yerr,
                fmt=MARKER[strat],
                color=COLOR[strat],
                markersize=NODE_MSIZE[nodes],
                capsize=3.5,
                linewidth=0,
                elinewidth=1.0,
                markeredgewidth=0.9,
                markeredgecolor="white",
                zorder=5,
            )

    # Lower ACF and higher throughput are jointly preferable.
    ax.annotate(
        "Better",
        xy=(0.30, 0.96),
        xytext=(0.43, 0.86),
        xycoords="axes fraction",
        textcoords="axes fraction",
        ha="center",
        va="center",
        fontsize=10.5,
        fontweight="bold",
        color="#444444",
        arrowprops=dict(
            arrowstyle="-|>",
            color="#444444",
            linewidth=1.5,
            mutation_scale=13,
        ),
        zorder=8,
    )

    # Two-part legend so method (color/shape) and scale (marker size) are
    # decoupled visually; otherwise the same "20K" tag appears 6 times in
    # the figure body and clutters the dense P1/P2/P3 cluster.
    method_legend = ax.legend(
        handles=_legend_handles(STRATEGIES),
        loc=(0.58, 0.50),
        title="Method",
        framealpha=0.92, edgecolor="#bbbbbb",
        labelspacing=0.45, handletextpad=0.6,
        borderpad=0.6,
    )
    ax.add_artist(method_legend)

    ax.legend(
        handles=_scale_legend_handles(),
        # Shifted right of the high-ACF/low-throughput cluster and its
        # horizontal whiskers, which reached the old box's lower-left corner.
        loc=(0.79, 0.15),
        title="Cluster size",
        framealpha=0.92, edgecolor="#bbbbbb",
        labelspacing=0.7, handletextpad=0.8,
        borderpad=0.6,
    )

    save_fig(fig, out_dir, "pareto-all-scales")


# ── main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Loading {CSV_PATH}")
    raw = load_data()
    raw = filter_duration_outliers(raw)
    data = aggregate(raw)
    data = inject_godel(data)

    print("\nAggregated medians (strategy, nodes) -> acf | throughput:")
    for strat in STRATEGIES:
        for nodes in NODE_SIZES:
            key = (strat, nodes)
            if key in data:
                d = data[key]
                print(f"  {strat:3s} {nodes:6d}n  acf={d['acf_med']:.4f}  tp={d['tp_med']:.1f} pods/s  n={d['n']}")

    print(f"\nGenerating figures → {OUT_DIR}")
    plot_all_scales(data, OUT_DIR)
    print("Done.")
