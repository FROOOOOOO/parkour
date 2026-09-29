#!/usr/bin/env python3
"""Figure `scalability-lowcontention`: throughput against cluster size.

One panel. Four arms are compared at 1,000-5,000 nodes, where contention is low
enough that parallel scheduling should scale almost freely: a single scheduler,
Vanilla with ten schedulers, ParKour with ten, and the Godel baseline. Each arm
draws its median with an interquartile band, so the overlap between Vanilla and
ParKour is visible rather than asserted.

Input
    experiments/work/figure-data/scalability-lowcontention.json, written by

        python ../scripts/export-figure-data.py --figure scalability-lowcontention

    which needs board B1 processed by `process-results.py`, and the Godel runs
    under `results/godel-new/`. This script holds no measurements of its own.

Usage
    python plot-scalability-lowcontention.py
    python plot-scalability-lowcontention.py --show
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

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402

FIGURE = "scalability-lowcontention"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")

RC_OVERRIDES = {
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

LINE_WIDTH = 1.4
MARKER_SIZE = 3.5
BAND_ALPHA = 0.12
GRID_KW = {"linestyle": "--", "alpha": 0.45, "linewidth": 0.6}

#: Arm key in the exported data -> (colour, marker, hollow marker, legend label)
ARMS = (
    ("single", style.NEUTRAL_GREY, "D", True, "single"),
    ("event-vanilla-diff", style.VANILLA_RED, "o", True, "Vanilla"),
    ("event-parkour-diff", style.PARKOUR_GREEN, "o", False, "ParKour"),
    ("godel", style.GODEL_PURPLE, "^", True, "Gödel"),
)


def _thousands(value: int) -> str:
    """Thin-space thousands separator, matching the paper body."""

    return "$" + f"{int(value):,}".replace(",", r"\,") + "$"


def _draw_band(axis, x, band, colour, marker, hollow) -> None:
    median = np.asarray(band["median"], dtype=float)
    q1 = np.asarray(band["q1"], dtype=float)
    q3 = np.asarray(band["q3"], dtype=float)

    axis.plot(
        x, median, color=colour, linestyle="-", linewidth=LINE_WIDTH,
        marker=marker, markersize=MARKER_SIZE,
        markerfacecolor="white" if hollow else colour,
    )
    axis.fill_between(
        x, np.maximum(0, q1), q3, color=colour, alpha=BAND_ALPHA, linewidth=0,
    )


def build_figure(data: dict):
    style.apply(**RC_OVERRIDES)

    figure = plt.figure(figsize=(3.4, 1.62))
    axis = figure.add_subplot(111)
    x = np.asarray(data["x"], dtype=float)

    for arm, colour, marker, hollow, _ in ARMS:
        band = data["series"].get(arm)
        if band is None:
            raise SystemExit(f"{FIGURE}: exported data has no arm {arm!r}")
        _draw_band(axis, x, band, colour, marker, hollow)

    axis.set_xticks(x)
    axis.set_xticklabels([_thousands(value) for value in data["x"]])
    axis.set_xlabel("Cluster size (nodes)")
    axis.set_ylabel("Throughput (pods/s)")
    axis.grid(True, **GRID_KW)
    axis.set_axisbelow(True)

    figure.legend(
        handles=[
            mlines.Line2D(
                [], [], color=colour, marker=marker, markersize=MARKER_SIZE,
                markerfacecolor="white" if hollow else colour,
                linewidth=LINE_WIDTH, label=label,
            )
            for _, colour, marker, hollow, label in ARMS
        ],
        loc="upper center", bbox_to_anchor=(0.5, 1.02), ncol=4, frameon=False,
        fontsize=6.5, columnspacing=1.0, handlelength=1.3, handletextpad=0.35,
    )
    figure.subplots_adjust(left=0.17, right=0.985, top=0.80, bottom=0.21)
    return figure


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=envelope.path_for(_EXPERIMENTS, FIGURE))
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--show", action="store_true")
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
