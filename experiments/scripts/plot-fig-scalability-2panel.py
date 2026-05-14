#!/usr/bin/env python3
"""
Paper Figure: Scalability — B1 low-contention (left) + B3 scheduler-count (right).

Layout: 1 row x 2 columns, 4:6 width ratio.
  Left  (B1): Cluster-size scalability under low contention, 3 data points
               x = node count, y = throughput (pods/s)
               E1 (grey dashed), E2 (dark red), E3 (dark green)
  Right (B3): Scheduler-count scalability under high contention, 5 data points
               x = scheduler count N, left-y = throughput (solid), right-y = ACF (dashed)
               E2/P1 (red tones from RdBu), E3/P4 (green); Event darker, Periodic lighter

Colours — fully aligned with paper_fig4 and paper_fig6:
  Grey  : #999999 (paper_fig6 COLOR_BL)
  Reds  : _rdbu[1] dark, _rdbu[3] light (paper_fig4 M_COLORS / V_COLORS)
  Greens: #238b45 dark, #74c476 light (paper_fig6 COLOR_PMC / COLOR_P)

Style: RC_PARAMS and layout conventions from simulation/paper_fig6.py.

Usage:
    python plot-fig-scalability.py           # save figure
    python plot-fig-scalability.py --show    # save + display
"""

import argparse
import os
import warnings

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

# ---------------------------------------------------------------------------
#  Colours — aligned with paper_fig4 (_rdbu) and paper_fig6 (greens)
# ---------------------------------------------------------------------------
_rdbu = sns.color_palette("RdBu", 11)

C_GREY    = "#999999"                          # single-scheduler (paper_fig6 COLOR_BL)
C_RED_D   = "#{:02x}{:02x}{:02x}".format(     # dark red  — E2 vanilla event
    int(_rdbu[1][0] * 255), int(_rdbu[1][1] * 255), int(_rdbu[1][2] * 255))
C_RED_L   = "#{:02x}{:02x}{:02x}".format(     # light red — P1 vanilla periodic
    int(_rdbu[3][0] * 255), int(_rdbu[3][1] * 255), int(_rdbu[3][2] * 255))
C_GREEN_D = "#238b45"                          # dark green  — E3 ParKour event   (COLOR_PMC)
C_GREEN_L = "#74c476"                          # light green — P4 ParKour periodic (COLOR_P)
C_PURPLE  = "#A78AB8"                          # light purple — Godel baseline

# ---------------------------------------------------------------------------
#  Style — exact match with simulation/paper_fig6.py RC_PARAMS
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":           9,
    "axes.labelsize":      9,
    "axes.titlesize":      8.5,
    "xtick.labelsize":     8,
    "ytick.labelsize":     8,
    "legend.fontsize":     8,
    "legend.title_fontsize": 9,
    "axes.linewidth":      0.8,
    "lines.linewidth":     1.5,
    "lines.markersize":    2.5,
}

LW_SOLID   = 1.5   # throughput line width
LW_DASHED  = 1.5   # ACF line width
ALPHA_BAND = 0.12

# ---------------------------------------------------------------------------
#  Data — hardcoded from eval-data.md §1.2 and §1.3
# ---------------------------------------------------------------------------

# B1: low-contention cluster-size scaling (x = nodes, y = tput pods/s)
B1_X        = [1000, 2000, 5000]
B1_X_LABELS = ["1K", "2K", "5K"]
B1_E1_TPUT  = [84.9, 55.9, 38.1]
B1_E2_TPUT  = [625.2, 546.8, 298.4]
B1_E3_TPUT  = [628.2, 548.0, 339.5]

# B1 error (±1 std from eval-data §1.2)
B1_E1_ERR   = [0.5, 0.4, 0]
B1_E2_ERR   = [4.6, 1.6, 58.7]
B1_E3_ERR   = [3.1, 3.4, 32.4]

# Godel B1 (N=10, low contention) — corrected medians + IQR/2 band
# from experiments/results/godel-new/ after backfill (sat-window divisor).
B1_GODEL_TPUT = [224.6, 209.0, 131.1]
B1_GODEL_ERR  = [2.0, 4.5, 0.1]

# B3: scheduler-count scalability (x = N, y1 = tput, y2 = ACF)
B3_X        = [2, 4, 6, 8, 10]
B3_X_LABELS = ["2", "4", "6", "8", "10"]

B3_E2_TPUT  = [52.2, 105.3, 150.9, 202.1, 247.9]
B3_E2_ACF   = [0.0079, 0.0206, 0.0348, 0.0456, 0.0536]
B3_E3_TPUT  = [51.3, 106.3, 159.0, 210.8, 260.8]
B3_E3_ACF   = [0.0001, 0.0021, 0.0048, 0.0072, 0.0137]
B3_P1_TPUT  = [34.7, 47.6, 52.2, 87.6, 92.6]
B3_P1_ACF   = [0.2370, 0.4875, 0.5854, 0.4451, 0.4697]
B3_P4_TPUT  = [48.4, 98.4, 133.8, 160.5, 212.3]
B3_P4_ACF   = [0.0937, 0.1156, 0.1462, 0.1623, 0.1134]

# Godel B3 (10K nodes, varying N) — corrected medians; static node partitioning
# caps tp regardless of N, see §Discussion for the architectural caveat.
B3_GODEL_TPUT = [213.2, 139.2, 208.7, 213.3, 161.7]
B3_GODEL_ACF  = [0.0021, 0.0026, 0.0018, 0.0023, 0.0024]


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def _draw_b1_panel(ax):
    """Left panel (40% width): B1 low-contention — throughput vs cluster size."""
    xpos = np.array(B1_X, dtype=float)

    # E1 single-scheduler (grey, diamond markers)
    ax.plot(xpos, B1_E1_TPUT, color=C_GREY, linestyle="-",
            linewidth=LW_SOLID, marker="D", markersize=3.5,
            markerfacecolor="white")
    ax.fill_between(xpos,
                    np.maximum(0, np.array(B1_E1_TPUT) - np.array(B1_E1_ERR)),
                    np.array(B1_E1_TPUT) + np.array(B1_E1_ERR),
                    color=C_GREY, alpha=ALPHA_BAND, linewidth=0)

    # E2 vanilla event (dark red, solid circle)
    ax.plot(xpos, B1_E2_TPUT, color=C_RED_D, linestyle="-",
            linewidth=LW_SOLID, marker="o", markersize=3.5,
            markerfacecolor="white")
    ax.fill_between(xpos,
                    np.maximum(0, np.array(B1_E2_TPUT) - np.array(B1_E2_ERR)),
                    np.array(B1_E2_TPUT) + np.array(B1_E2_ERR),
                    color=C_RED_D, alpha=ALPHA_BAND, linewidth=0)

    # E3 ParKour event (dark green, solid circle)
    ax.plot(xpos, B1_E3_TPUT, color=C_GREEN_D, linestyle="-",
            linewidth=LW_SOLID, marker="o", markersize=3.5)
    ax.fill_between(xpos,
                    np.maximum(0, np.array(B1_E3_TPUT) - np.array(B1_E3_ERR)),
                    np.array(B1_E3_TPUT) + np.array(B1_E3_ERR),
                    color=C_GREEN_D, alpha=ALPHA_BAND, linewidth=0)

    # Godel (light purple, triangle) — production baseline with static partitioning
    ax.plot(xpos, B1_GODEL_TPUT, color=C_PURPLE, linestyle="-",
            linewidth=LW_SOLID, marker="^", markersize=3.5,
            markerfacecolor="white")
    ax.fill_between(xpos,
                    np.maximum(0, np.array(B1_GODEL_TPUT) - np.array(B1_GODEL_ERR)),
                    np.array(B1_GODEL_TPUT) + np.array(B1_GODEL_ERR),
                    color=C_PURPLE, alpha=ALPHA_BAND, linewidth=0)

    ax.set_xticks(xpos)
    ax.set_xticklabels(B1_X_LABELS)
    ax.set_xlabel("Cluster size (nodes)")
    ax.set_ylabel("TP (pods/s)")
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


def _draw_b3_panel(ax):
    """Right panel (60% width): B3 scheduler-count — throughput (solid, left y)
       + ACF (dashed, right y)."""
    ax2 = ax.twinx()
    x = np.array(B3_X, dtype=float)

    # ── Throughput (left y, solid) ───────────────────────────────────────
    ax.plot(x, B3_E2_TPUT, color=C_RED_D, linestyle="-",
            linewidth=LW_SOLID, marker="o", markersize=3.5,
            markerfacecolor="white")
    ax.plot(x, B3_E3_TPUT, color=C_GREEN_D, linestyle="-",
            linewidth=LW_SOLID, marker="o", markersize=3.5)
    ax.plot(x, B3_P1_TPUT, color=C_RED_L, linestyle="-",
            linewidth=LW_SOLID, marker="s", markersize=3.5,
            markerfacecolor="white")
    ax.plot(x, B3_P4_TPUT, color=C_GREEN_L, linestyle="-",
            linewidth=LW_SOLID, marker="s", markersize=3.5)
    # Godel: solid tp line, triangle marker
    ax.plot(x, B3_GODEL_TPUT, color=C_PURPLE, linestyle="-",
            linewidth=LW_SOLID, marker="^", markersize=3.5,
            markerfacecolor="white")

    # ── ACF (right y, dashed) ───────────────────────────────────────────
    ax2.plot(x, B3_E2_ACF, color=C_RED_D, linestyle=":",
             linewidth=LW_DASHED, marker="o", markersize=3.5,
             markerfacecolor="white")
    ax2.plot(x, B3_E3_ACF, color=C_GREEN_D, linestyle=":",
             linewidth=LW_DASHED, marker="o", markersize=3.5)
    ax2.plot(x, B3_P1_ACF, color=C_RED_L, linestyle=":",
             linewidth=LW_DASHED, marker="s", markersize=3.5,
             markerfacecolor="white")
    ax2.plot(x, B3_P4_ACF, color=C_GREEN_L, linestyle=":",
             linewidth=LW_DASHED, marker="s", markersize=3.5)
    ax2.plot(x, B3_GODEL_ACF, color=C_PURPLE, linestyle=":",
             linewidth=LW_DASHED, marker="^", markersize=3.5,
             markerfacecolor="white")

    # ── Axis styling ─────────────────────────────────────────────────────
    ax.set_xticks(x)
    ax.set_xticklabels(B3_X_LABELS)
    ax.set_xlabel("Number of schedulers")
    ax.set_ylabel("TP (pods/s)")

    ax2.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax2.set_ylabel("Conflict rate")
    ax2.set_ylim(bottom=-0.02, top=0.65)

    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


# ---------------------------------------------------------------------------
#  Build figure
# ---------------------------------------------------------------------------

def build_figure():
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    # Full-width double-column figure, 4:6 width ratio
    fig = plt.figure(figsize=(3.5, 1.8))
    gs = GridSpec(1, 2, figure=fig, width_ratios=[0.4, 0.6],
                  hspace=0, wspace=0.45,
                  left=0.08, right=0.97, top=0.80, bottom=0.18)

    ax_left  = fig.add_subplot(gs[0, 0])
    ax_right = fig.add_subplot(gs[0, 1])

    _draw_b1_panel(ax_left)
    _draw_b3_panel(ax_right)

    # ── Panel labels ─────────────────────────────────────────────────────
    # ax_left.set_title("Low-contention (B1)", fontsize=8.5, fontweight="bold",
    #                   loc="left", pad=2)
    # ax_right.set_title("High-contention (B3)", fontsize=8.5, fontweight="bold",
    #                    loc="left", pad=2)

    # ── Shared legend (above figure, 2 rows, centered) ───────────────────
    # Row 1: configurations
    h_e1 = mlines.Line2D([], [], color=C_GREY, linestyle="-",
                         linewidth=LW_SOLID, marker="D", markersize=3.5,
                         markerfacecolor="white", label="Single")
    h_e2 = mlines.Line2D([], [], color=C_RED_D, linestyle="-",
                         linewidth=LW_SOLID, marker="o", markersize=3.5,
                         markerfacecolor="white", label="Vanilla-Event")
    h_e3 = mlines.Line2D([], [], color=C_GREEN_D, linestyle="-",
                         linewidth=LW_SOLID, marker="o", markersize=3.5,
                         label="ParKour-Event")
    h_p1 = mlines.Line2D([], [], color=C_RED_L, linestyle="-",
                         linewidth=LW_SOLID, marker="s", markersize=3.5,
                         markerfacecolor="white", label="Vanilla-Periodic")
    h_p4 = mlines.Line2D([], [], color=C_GREEN_L, linestyle="-",
                         linewidth=LW_SOLID, marker="s", markersize=3.5,
                         label="ParKour-Periodic")
    h_g  = mlines.Line2D([], [], color=C_PURPLE, linestyle="-",
                         linewidth=LW_SOLID, marker="^", markersize=3.5,
                         markerfacecolor="white", label="Godel")

    # Row 2: metric style
    h_tput = mlines.Line2D([], [], color="#333333", linestyle="-",
                           linewidth=LW_SOLID, label="TP")
    h_acf  = mlines.Line2D([], [], color="#333333", linestyle=":",
                           linewidth=LW_DASHED, label="Conflict rate")

    fig.legend(handles=[h_e1, h_e3, h_e2, h_p4, h_p1, h_g],
               loc=(0.06, 0.8), ncol=3,
               columnspacing=0.6, handlelength=1.4,
               handletextpad=0.3, frameon=False, fontsize=7.5)
    fig.legend(handles=[h_tput, h_acf],
               loc=(0.76, 0.8),
               ncol=1, columnspacing=1.5, handlelength=1.4,
               handletextpad=0.3, frameon=False, fontsize=7.5)

    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"scalability.{ext}")
        fig.savefig(p, bbox_inches="tight")
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure — B1+B3 scalability (1×2 panels, 4:6 ratio)")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    args = ap.parse_args()

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
