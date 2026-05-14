#!/usr/bin/env python3
"""
Paper Figure: Parameter Robustness (RQ §5.6).

Layout: 1 row x 2 columns.
  Left  (K-sweep): ACF vs K in {0, 1, 2, 4} at fixed w=0.3.
                    Lines: Event-diff, Periodic-glob (P4 env),
                           Periodic-same, Periodic-diff.
  Right (w-sweep): ACF vs w in {0, 0.1, 0.3, 0.5, 0.7} at fixed K=4.
                    Lines: Periodic-glob, Periodic-same, Periodic-diff.

Star markers highlight the simulation-recommended defaults (K=2, w=0.5).
The visual story is "any K>=1 and any w>=0.1 yields large ACF reduction across
all sync patterns; defaults are not single sweet spots".

Colours / style aligned with plot-fig-scalability.py.

Usage:
    python plot-fig-robustness.py
    python plot-fig-robustness.py --show
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
#  Colours
# ---------------------------------------------------------------------------
C_EVENT      = "#4575b4"   # blue (event/diff)
C_PER_GLOB   = "#1b7837"   # dark green (periodic/glob, == P4 env)
C_PER_SAME   = "#5aae61"   # mid green  (periodic/same)
C_PER_DIFF   = "#a6dba0"   # light green(periodic/diff)
C_DEFAULT    = "#d73027"   # red star highlight for sim default

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":           9,
    "axes.labelsize":      9,
    "axes.titlesize":      8.5,
    "xtick.labelsize":     8,
    "ytick.labelsize":     8,
    "legend.fontsize":     7.5,
    "axes.linewidth":      0.8,
    "lines.linewidth":     1.4,
    "lines.markersize":    3.5,
}

# ---------------------------------------------------------------------------
#  Data — eval-data.md §5.1 (K) and §5.3 (w)
# ---------------------------------------------------------------------------

# K-sweep (w=0.3 fixed)
K_X        = [0, 1, 2, 4]
K_E_DIFF   = [0.0611, 0.0220, 0.0118, 0.0073]
K_P_GLOB   = [0.3072, 0.1457, 0.1008, 0.0687]
K_P_SAME   = [0.4972, 0.3780, 0.2910, 0.1403]
K_P_DIFF   = [0.5316, 0.3853, 0.2941, 0.2000]

# w-sweep (K=4 fixed)
W_X        = [0.0, 0.1, 0.3, 0.5, 0.7]
W_E_DIFF   = [0.0069, 0.0063, 0.0065, 0.0069, 0.0073]   # event/diff (medians from results/p/)
W_P_GLOB   = [0.3144, 0.0697, 0.0966, 0.0616, 0.0679]
W_P_SAME   = [0.5002, 0.2058, 0.1473, 0.1786, 0.1482]
W_P_DIFF   = [0.4157, 0.1661, 0.1451, 0.1530, 0.1464]


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def _draw_k_panel(ax):
    x = np.array(K_X, dtype=float)

    ax.plot(x, K_E_DIFF, color=C_EVENT,    marker="o", markerfacecolor="white")
    ax.plot(x, K_P_GLOB, color=C_PER_GLOB, marker="s")
    ax.plot(x, K_P_SAME, color=C_PER_SAME, marker="^")
    ax.plot(x, K_P_DIFF, color=C_PER_DIFF, marker="D")

    ax.set_xticks(x)
    ax.set_xticklabels([str(v) for v in K_X])
    ax.set_xlabel("$K$ (multi-candidate)")
    ax.set_ylabel("Conflict rate (ACF)")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_ylim(-0.02, 0.6)
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


def _draw_w_panel(ax):
    x = np.array(W_X, dtype=float)

    ax.plot(x, W_E_DIFF, color=C_EVENT,    marker="o", markerfacecolor="white")
    ax.plot(x, W_P_GLOB, color=C_PER_GLOB, marker="s")
    ax.plot(x, W_P_SAME, color=C_PER_SAME, marker="^")
    ax.plot(x, W_P_DIFF, color=C_PER_DIFF, marker="D")

    ax.set_xticks(x)
    ax.set_xticklabels([f"{v:g}" for v in W_X])
    ax.set_xlabel("$w$ (penalty weight)")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_ylim(-0.02, 0.6)
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


# ---------------------------------------------------------------------------
#  Build figure
# ---------------------------------------------------------------------------

def build_figure():
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    fig = plt.figure(figsize=(3.5, 1.95))
    gs = GridSpec(1, 2, figure=fig, width_ratios=[0.5, 0.5],
                  wspace=0.30,
                  left=0.12, right=0.97, top=0.78, bottom=0.20)

    ax_k = fig.add_subplot(gs[0, 0])
    ax_w = fig.add_subplot(gs[0, 1])

    _draw_k_panel(ax_k)
    _draw_w_panel(ax_w)

    # Suppress redundant y-tick labels on right panel
    ax_w.set_yticklabels([])

    # Shared legend (top, 2 rows)
    h_event   = mlines.Line2D([], [], color=C_EVENT,    marker="o",
                              markerfacecolor="white", linewidth=1.4,
                              label="vanilla-E")
    h_p_glob  = mlines.Line2D([], [], color=C_PER_GLOB, marker="s",
                              linewidth=1.4, label="vanilla-P")
    h_p_same  = mlines.Line2D([], [], color=C_PER_SAME, marker="^",
                              linewidth=1.4, label="sameSync")
    h_p_diff  = mlines.Line2D([], [], color=C_PER_DIFF, marker="D",
                              linewidth=1.4, label="diffSync")
    fig.legend(handles=[h_event, h_p_glob, h_p_same, h_p_diff],
               loc=(0.2, 0.85), ncol=4,
               columnspacing=0.8, handlelength=1.4,
               handletextpad=0.3, frameon=False, fontsize=7.5)

    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"robustness.{ext}")
        fig.savefig(p, bbox_inches="tight", pad_inches=0)
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure — Parameter robustness (K-sweep | w-sweep)")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    args = ap.parse_args()

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
