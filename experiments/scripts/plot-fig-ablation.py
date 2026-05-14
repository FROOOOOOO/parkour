#!/usr/bin/env python3
"""
Paper Figure: Cross-Paradigm Ablation (RQ3).

Layout: 1 row x 1 column grouped bar chart.
  X axis: 4 configs (Ab0 baseline, Ab1 +M, Ab2 +P, Ab3 +M+P)
  Per group: 2 bars (Event, Periodic)
  Y axis : ACF rate (%)
  Bar-top annotation: throughput (pods/s)

The visual story is the M+P asymmetry: Event drops to near-zero with M alone
(Ab1=Ab3), but Periodic only collapses with M+P jointly (Ab3 << min(Ab1, Ab2)).

Colours / style aligned with plot-fig-scalability.py and simulation/paper_fig6.py.

Usage:
    python plot-fig-ablation.py            # save figure
    python plot-fig-ablation.py --show     # save + display
"""

import argparse
import os
import warnings

import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.ticker import PercentFormatter

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
FIG_DIR = os.path.join(_REPO, "paper", "figs")

# ---------------------------------------------------------------------------
#  Colours — Event = dark red, Periodic = dark green
#  (matches scalability fig: ParKour-Event green, Vanilla-Event red, etc.;
#   here we instead use red for Event-as-paradigm and green for Periodic-as-
#   paradigm, with intensity encoding +M+P highlight via hatch)
# ---------------------------------------------------------------------------
_rdbu = sns.color_palette("RdBu", 11)

C_EVENT    = "#{:02x}{:02x}{:02x}".format(int(_rdbu[1][0] * 255),
                                          int(_rdbu[1][1] * 255),
                                          int(_rdbu[1][2] * 255))   # dark red
C_PERIODIC = "#{:02x}{:02x}{:02x}".format(int(_rdbu[3][0] * 255),
                                          int(_rdbu[3][1] * 255),
                                          int(_rdbu[3][2] * 255))   # light red
# Override Periodic to a distinct hue to avoid collapsing into red ramp:
C_EVENT    = "#4575b4"   # blue (paper_fig palette compatible)
C_PERIODIC = "#fd8d3c"   # orange

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":           9,
    "axes.labelsize":      9,
    "axes.titlesize":      8.5,
    "xtick.labelsize":     8.5,
    "ytick.labelsize":     8,
    "legend.fontsize":     8,
    "legend.title_fontsize": 9,
    "axes.linewidth":      0.8,
}

# ---------------------------------------------------------------------------
#  Data — eval-data.md §2.1
# ---------------------------------------------------------------------------
LABELS = ["Ab0\n(baseline)", "Ab1\n(+M)", "Ab2\n(+P)", "Ab3\n(+M+P)"]
ACF_E  = [0.0552, 0.0137, 0.0542, 0.0141]
ACF_P  = [0.5081, 0.3550, 0.3613, 0.1025]
TPUT_E = [245.7, 263.7, 244.8, 260.6]
TPUT_P = [ 94.2, 119.7, 140.6, 213.5]


# ---------------------------------------------------------------------------
#  Drawing
# ---------------------------------------------------------------------------

def build_figure():
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    fig, ax = plt.subplots(figsize=(3.5, 1.95))
    fig.subplots_adjust(left=0.13, right=0.97, top=0.83, bottom=0.20)

    n = len(LABELS)
    x = np.arange(n, dtype=float)
    bw = 0.36

    # Event bars
    bars_e = ax.bar(x - bw / 2, ACF_E, width=bw,
                    color=C_EVENT, edgecolor="black", linewidth=0.4,
                    label="Event")
    # Periodic bars
    bars_p = ax.bar(x + bw / 2, ACF_P, width=bw,
                    color=C_PERIODIC, edgecolor="black", linewidth=0.4,
                    label="Periodic")

    # Hatch on Ab3 (+M+P) to highlight ParKour
    bars_e[-1].set_hatch("//")
    bars_p[-1].set_hatch("//")

    # Throughput annotations (pods/s) on bar tops
    def _annotate(bars, vals, color):
        for bar, v in zip(bars, vals):
            h = bar.get_height()
            ax.annotate(f"{v:.0f}",
                        xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 1.5), textcoords="offset points",
                        ha="center", va="bottom",
                        fontsize=6.5, color=color)

    _annotate(bars_e, TPUT_E, "#1c3f6e")
    _annotate(bars_p, TPUT_P, "#a85a16")

    # X / Y axes
    ax.set_xticks(x)
    ax.set_xticklabels(LABELS)
    ax.set_ylabel("Conflict rate (ACF)")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_ylim(0, 0.62)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)

    # Legend
    h_event    = mpatches.Patch(facecolor=C_EVENT,    edgecolor="black",
                                linewidth=0.4, label="Event")
    h_periodic = mpatches.Patch(facecolor=C_PERIODIC, edgecolor="black",
                                linewidth=0.4, label="Periodic")
    h_parkour  = mpatches.Patch(facecolor="white",    edgecolor="black",
                                linewidth=0.4, hatch="//", label="ParKour (Ab3)")
    ax.legend(handles=[h_event, h_periodic, h_parkour],
              loc="upper right", ncol=3,
              columnspacing=0.8, handlelength=1.4,
              handletextpad=0.4, frameon=False, fontsize=7.5,
              bbox_to_anchor=(1.0, 1.18))

    # Inline note: throughput numbers
    ax.text(0.005, 0.97, "Bar-top: throughput (pods/s)",
            transform=ax.transAxes, fontsize=6.5, color="#444444",
            ha="left", va="top", style="italic")

    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"ablation.{ext}")
        fig.savefig(p, bbox_inches="tight")
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure — Cross-paradigm ablation (Ab0..Ab3)")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    args = ap.parse_args()

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
