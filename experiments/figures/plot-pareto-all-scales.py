#!/usr/bin/env python3
"""Figure `pareto-all-scales`: the throughput/ACF trade-off across cluster sizes.

One scatter panel. Each method contributes four points, one per cluster size,
joined by a dashed trajectory that runs from 2,000 to 20,000 nodes; marker size
encodes the size so the scale is legible without per-point labels, which collide
in the dense high-ACF cluster. Both axes carry interquartile whiskers.

The legend is split in two. Colour and shape carry the method, marker size
carries the cluster size; keeping them separate avoids repeating the same size
tag once per method inside an already crowded figure body.

Input
    experiments/work/figure-data/pareto-all-scales.json, written by

        python ../scripts/export-figure-data.py --figure pareto-all-scales

    which needs board B2 processed by `process-results.py`, and the Godel runs
    under `results/godel-new/`. This script holds no measurements of its own.

Usage
    python plot-pareto-all-scales.py
    python plot-pareto-all-scales.py --show
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as ticker  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.legend import Legend  # noqa: E402

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402

FIGURE = "pareto-all-scales"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")

# This figure compares six configurations at once, so it needs a wider set of
# hues than the shared palette carries: one family per mechanism, with the
# event-driven member lighter and the periodic member darker. Only the Godel
# colour is shared with the other figures.
ARMS = (
    ("event-vanilla-diff", "#F4845F", "o", "Vanilla (event-driven)"),
    ("periodic-vanilla-glob", "#A01010", "s", "Vanilla (periodic)"),
    ("periodic-vanilla-same", "#7BAFD4", "^", "sameSync"),
    ("periodic-vanilla-diff", "#1A4E8A", "v", "diffSync"),
    ("event-parkour-diff", "#6DBF67", "D", "ParKour (event-driven)"),
    ("periodic-parkour-glob", "#1A6B2A", "p", "ParKour (periodic)"),
    ("godel", style.GODEL_PURPLE, "8", "Gödel"),
)

RC_OVERRIDES = {
    "axes.labelsize": 12,
    "axes.titlesize": 12,
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "legend.fontsize": 10.5,
    "legend.title_fontsize": 11,
}

GRID_KW = {"linestyle": "--", "alpha": 0.45, "linewidth": 0.8}

#: Marker size per cluster size: small is 2k, large is 20k.
NODE_MARKER_SIZE = {2000: 5, 5000: 8, 10000: 11, 20000: 14}


def _size_label(nodes: int) -> str:
    return f"{nodes // 1000}k"


def _asymmetric_error(point: dict, metric: str) -> np.ndarray:
    """Whisker lengths below and above the median, as errorbar wants them."""

    stats = point[metric]
    median = stats["median"]
    return np.array([[median - stats["q1"]], [stats["q3"] - median]])


def build_figure(data: dict):
    style.apply(context="paper", font_scale=1.3, **RC_OVERRIDES)

    figure, axis = plt.subplots(figsize=(6.5, 4.8))
    axis.set_xlabel("ACF rate")
    axis.set_ylabel("Throughput (pods/s)")
    axis.xaxis.set_major_formatter(ticker.PercentFormatter(1.0, decimals=0))
    axis.grid(True, **GRID_KW, zorder=0)

    node_counts = data["x"]
    for key, colour, marker, _ in ARMS:
        points = data["series"].get(key)
        if points is None:
            raise SystemExit(f"{FIGURE}: exported data has no arm {key!r}")

        drawn = [
            (nodes, point)
            for nodes, point in zip(node_counts, points)
            if point["acf"]["median"] is not None
            and point["throughput"]["median"] is not None
        ]
        if len(drawn) >= 2:
            axis.plot(
                [point["acf"]["median"] for _, point in drawn],
                [point["throughput"]["median"] for _, point in drawn],
                color=colour, linewidth=1.2, linestyle="--", alpha=0.55, zorder=3,
            )

        for nodes, point in drawn:
            axis.errorbar(
                point["acf"]["median"],
                point["throughput"]["median"],
                xerr=_asymmetric_error(point, "acf"),
                yerr=_asymmetric_error(point, "throughput"),
                fmt=marker, color=colour,
                markersize=NODE_MARKER_SIZE[nodes],
                capsize=3.5, linewidth=0, elinewidth=1.0,
                markeredgewidth=0.9, markeredgecolor="white", zorder=5,
            )

    # Lower ACF and higher throughput are jointly preferable.
    axis.annotate(
        "Better",
        xy=(0.30, 0.96), xytext=(0.43, 0.86),
        xycoords="axes fraction", textcoords="axes fraction",
        ha="center", va="center", fontsize=10.5, fontweight="bold",
        color="#444444",
        arrowprops=dict(arrowstyle="-|>", color="#444444", linewidth=1.5,
                        mutation_scale=13),
        zorder=8,
    )

    method_legend = axis.legend(
        handles=[
            plt.Line2D([0], [0], marker=marker, color=colour, markersize=8,
                       linewidth=0, markeredgecolor="white",
                       markeredgewidth=0.9, label=label)
            for _, colour, marker, label in ARMS
        ],
        loc=(0.58, 0.50), title="Method", framealpha=0.92, edgecolor="#bbbbbb",
        labelspacing=0.45, handletextpad=0.6, borderpad=0.6,
    )
    axis.add_artist(method_legend)

    axis.legend(
        handles=[
            plt.Line2D([0], [0], marker="o", color="#888888",
                       markersize=NODE_MARKER_SIZE[nodes], linewidth=0,
                       markeredgecolor="white", markeredgewidth=0.9,
                       label=_size_label(nodes))
            for nodes in node_counts
        ],
        # Shifted right of the high-ACF cluster and its horizontal whiskers,
        # which reached the previous box's lower-left corner.
        loc=(0.79, 0.15), title="Cluster size", framealpha=0.92,
        edgecolor="#bbbbbb", labelspacing=0.7, handletextpad=0.8, borderpad=0.6,
    )
    return figure


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=envelope.path_for(_EXPERIMENTS, FIGURE))
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    figure = build_figure(envelope.load(args.data, figure=FIGURE))
    for path in style.save_figure(
        figure, args.output_dir, FIGURE,
        pad_inches=0, dpi=200,
        # Legends sit outside the axes; without them the tight bounding box
        # clips the longer method labels on the right edge.
        bbox_extra_artists=list(figure.findobj(Legend)),
    ):
        print(path)

    if args.show:
        plt.show()
    else:
        plt.close(figure)


if __name__ == "__main__":
    main()
