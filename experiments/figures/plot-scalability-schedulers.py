#!/usr/bin/env python3
"""Figure `scalability-schedulers`: conflict and throughput against instance count.

Two stacked panels, one per synchronization paradigm, sweeping 2-10 scheduler
instances at 10,000 nodes. Within a panel, ACF is drawn as grouped bars against
the left axis and throughput as dotted lines with open markers against the right.

Separating the paradigms means colour no longer has to encode the paradigm, so
one hue per system suffices and each throughput marker sits directly above the
bar it belongs to. The throughput axis is shared between panels so the two are
comparable; the ACF axes stay independent because the two ranges differ by an
order of magnitude, which is itself the result.

Input
    experiments/work/figure-data/scalability-schedulers.json, written by

        python ../scripts/export-figure-data.py --figure scalability-schedulers

    which needs board B3 processed by `process-results.py`, and the Godel runs
    under `results/godel-new/`. This script holds no measurements of its own.

Usage
    python plot-scalability-schedulers.py
    python plot-scalability-schedulers.py --show
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
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import MaxNLocator, PercentFormatter  # noqa: E402

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402

FIGURE = "scalability-schedulers"
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

BAR_ALPHA = 0.9
LW_TPUT = 1.3
MS_TPUT = 3.6
MEW_TPUT = 0.9
GRID_KW = {"linestyle": "--", "alpha": 0.45, "linewidth": 0.6}

#: (panel title, [(arm key, colour)]) in draw order. Dark shades are the
#: event-driven paradigm, light shades the periodic one.
PANELS = (
    (
        "(a) Event-driven",
        (
            ("event-vanilla-diff", style.VANILLA_RED),
            ("event-parkour-diff", style.PARKOUR_GREEN),
            ("godel", style.GODEL_PURPLE),
        ),
    ),
    (
        "(b) Periodic",
        (
            ("periodic-vanilla-glob", style.VANILLA_RED_LIGHT),
            ("periodic-parkour-glob", style.PARKOUR_GREEN_LIGHT),
        ),
    ),
)

LEGEND = (
    (style.VANILLA_RED, "Vanilla (event-driven)"),
    (style.VANILLA_RED_LIGHT, "Vanilla (periodic)"),
    (style.PARKOUR_GREEN, "ParKour (event-driven)"),
    (style.PARKOUR_GREEN_LIGHT, "ParKour (periodic)"),
    (style.GODEL_PURPLE, "Gödel"),
)


def _arm(data: dict, key: str) -> dict:
    series = data["series"].get(key)
    if series is None:
        raise SystemExit(f"{FIGURE}: exported data has no arm {key!r}")
    return series


def _draw_paradigm(axis, data, arms, *, title, show_xticklabels, tput_top) -> None:
    right = axis.twinx()
    x = np.arange(len(data["x"]), dtype=float)
    width = 0.78 / len(arms)

    peak_acf = 0.0
    for index, (key, colour) in enumerate(arms):
        series = _arm(data, key)
        offset = (index - (len(arms) - 1) / 2.0) * width
        axis.bar(
            x + offset, series["acf"], width=width, color=colour,
            edgecolor=style.EDGE_GREY, linewidth=0.4, alpha=BAR_ALPHA, zorder=2,
        )
        right.plot(
            x + offset, series["throughput"], color=colour, linestyle=":",
            linewidth=LW_TPUT, marker="o", markersize=MS_TPUT,
            markerfacecolor="white", markeredgecolor=style.EDGE_GREY,
            markeredgewidth=MEW_TPUT, zorder=3,
        )
        peak_acf = max(peak_acf, max(series["acf"]))

    axis.set_xticks(x)
    axis.set_xticklabels([str(v) for v in data["x"]] if show_xticklabels else [])
    axis.set_ylabel("ACF rate")
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.yaxis.set_major_locator(MaxNLocator(nbins=4))
    # Headroom so the tallest bar does not sit above the last labelled tick.
    axis.set_ylim(0, peak_acf * 1.18)
    axis.grid(True, axis="y", **GRID_KW)
    axis.set_axisbelow(True)
    axis.set_title(title, loc="left", pad=3)

    right.set_ylabel("Throughput\n(pods/s)")
    right.yaxis.set_major_locator(MaxNLocator(nbins=4))
    right.set_ylim(0, tput_top)


def build_figure(data: dict):
    style.apply(**RC_OVERRIDES)

    figure = plt.figure(figsize=(3.4, 3.05))
    grid = GridSpec(
        2, 1, figure=figure, hspace=0.24,
        # The legend takes three rows, so the top margin is generous; the figure
        # height is held fixed to avoid costing a page.
        left=0.17, right=0.80, top=0.795, bottom=0.13,
    )

    tput_top = max(
        max(_arm(data, key)["throughput"])
        for _, arms in PANELS
        for key, _ in arms
    ) * 1.12

    for index, (title, arms) in enumerate(PANELS):
        axis = figure.add_subplot(grid[index, 0])
        last = index == len(PANELS) - 1
        _draw_paradigm(
            axis, data, arms, title=title, show_xticklabels=last, tput_top=tput_top,
        )
        if last:
            axis.set_xlabel("Number of schedulers")

    handles = [
        mpatches.Patch(
            facecolor=colour, edgecolor=style.EDGE_GREY, linewidth=0.4,
            alpha=BAR_ALPHA, label=label,
        )
        for colour, label in LEGEND
    ]
    handles.append(
        mlines.Line2D(
            [], [], color="#555555", linestyle=":", linewidth=LW_TPUT,
            marker="o", markersize=MS_TPUT, markerfacecolor="white",
            markeredgecolor=style.EDGE_GREY, markeredgewidth=MEW_TPUT,
            label="Throughput (right axis)",
        )
    )
    figure.legend(
        handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0), ncol=2,
        frameon=False, fontsize=6.5, columnspacing=1.0, handlelength=1.3,
        handletextpad=0.35,
    )
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
