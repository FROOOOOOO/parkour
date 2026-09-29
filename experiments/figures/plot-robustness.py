#!/usr/bin/env python3
"""Figure `robustness`: parameter sensitivity of the conflict rate.

Layout is one row of two panels:

  left   ACF against candidate list length K, at fixed feedback rate w = 0.3
  right  ACF against feedback rate w, at fixed K = 5

Each panel carries four arms: event-driven, and the three periodic sync
patterns (glob, same, diff). The point of the figure is that no arm depends on
hitting a single sweet spot: any K >= 2 and any w >= 0.1 already removes most of
the conflicts.

Input
    experiments/work/figure-data/robustness.json, written by

        python ../scripts/export-figure-data.py --figure robustness

    which needs boards K and P processed by `process-results.py` first. This
    script holds no measurements of its own.

Usage
    python plot-robustness.py
    python plot-robustness.py --show
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

import matplotlib.lines as mlines  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402

FIGURE = "robustness"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")

# The event arm is the shared paradigm colour; the three periodic shades are a
# ramp private to this figure, so they stay here rather than in the palette.
C_EVENT = style.EVENT_BLUE
C_PER_GLOB = "#1b7837"   # dark green  (periodic/glob)
C_PER_SAME = "#5aae61"   # mid green   (periodic/same)
C_PER_DIFF = "#a6dba0"   # light green (periodic/diff)

# Per-figure sizes. The shared font family and embedding settings come from
# common.style; only what is specific to this figure's column width is here.
RC_OVERRIDES = {
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 8.5,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 7.5,
    "axes.linewidth": 0.8,
    "lines.linewidth": 1.4,
    "lines.markersize": 3.5,
}

#: Arm key in the exported data -> (colour, marker, hollow marker, legend label)
ARMS = (
    ("event", C_EVENT, "o", True, "Event-driven"),
    ("periodic-glob", C_PER_GLOB, "s", False, "Periodic"),
    ("periodic-same", C_PER_SAME, "^", False, "sameSync"),
    ("periodic-diff", C_PER_DIFF, "D", False, "diffSync"),
)

Y_LIMITS = (-0.02, 0.6)
GRID_KW = {"linestyle": "--", "alpha": 0.45, "linewidth": 0.6}


def _draw_sweep(axis, sweep: dict, x_values, tick_labels, xlabel: str) -> None:
    x = np.asarray(x_values, dtype=float)
    for arm, colour, marker, hollow, _ in ARMS:
        values = sweep["series"].get(arm)
        if values is None:
            raise SystemExit(f"{FIGURE}: exported data has no arm {arm!r}")
        axis.plot(
            x,
            values,
            color=colour,
            marker=marker,
            markerfacecolor="white" if hollow else colour,
        )

    axis.set_xticks(x)
    axis.set_xticklabels(tick_labels)
    axis.set_xlabel(xlabel)
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.set_ylim(*Y_LIMITS)
    axis.grid(True, **GRID_KW)


def build_figure(data: dict):
    style.apply(**RC_OVERRIDES)

    figure = plt.figure(figsize=(3.5, 1.75))
    grid = GridSpec(
        1, 2, figure=figure, width_ratios=[0.5, 0.5], wspace=0.30,
        left=0.12, right=0.97, top=0.82, bottom=0.21,
    )
    ax_k = figure.add_subplot(grid[0, 0])
    ax_w = figure.add_subplot(grid[0, 1])

    # The runner records backup counts; the paper counts the whole candidate
    # list. Only the tick positions shift, the measured series are untouched.
    k_sweep = data["k_sweep"]
    k_paper = [int(value) + 1 for value in k_sweep["x"]]
    _draw_sweep(ax_k, k_sweep, k_paper, [str(v) for v in k_paper],
                "Candidate list length $K$")
    ax_k.set_ylabel("ACF rate")

    w_sweep = data["w_sweep"]
    _draw_sweep(ax_w, w_sweep, w_sweep["x"], [f"{v:g}" for v in w_sweep["x"]],
                "Feedback rate $w$")
    ax_w.set_yticklabels([])

    handles = [
        mlines.Line2D(
            [], [], color=colour, marker=marker, linewidth=1.4,
            markerfacecolor="white" if hollow else colour, label=label,
        )
        for _, colour, marker, hollow, label in ARMS
    ]
    figure.legend(
        handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.99), ncol=4,
        columnspacing=0.8, handlelength=1.3, handletextpad=0.25,
        frameon=False, fontsize=7.0,
    )
    return figure


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=envelope.path_for(_EXPERIMENTS, FIGURE),
                        help="Exported figure data (default: the export path)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--show", action="store_true",
                        help="Display the figure after saving")
    args = parser.parse_args()

    figure = build_figure(envelope.load(args.data, figure=FIGURE))
    for path in style.save_figure(figure, args.output_dir, FIGURE, pad_inches=0):
        print(path)

    if args.show:
        plt.show()
    else:
        plt.close(figure)


if __name__ == "__main__":
    main()
