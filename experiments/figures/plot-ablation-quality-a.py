#!/usr/bin/env python3
"""Figure `ablation-quality-a`: conflict rate per mechanism combination.

One panel, four groups: Vanilla, multi-candidate only, penalty only, and both.
Each group carries four bars, two per paradigm, because two conflict measures
are contrasted:

  ACF  a pod whose whole candidate list failed, escalated to a full reschedule
  BCR  candidate-level bind failures the binder absorbed without rescheduling

They coincide when K=0 and separate once a fallback list exists, which is what
the panel is for. The number above each ACF bar is that cell's median
throughput; the axis is labelled with the quantity the bars share rather than
with either metric's name, and the caption says what the bar-top numbers mean.

Input
    experiments/work/figure-data/ablation-quality-a.json, written by

        python ../scripts/export-figure-data.py --figure ablation-quality-a

    which needs board C processed by `process-results.py`. This script holds no
    measurements of its own.

Usage
    python plot-ablation-quality-a.py
    python plot-ablation-quality-a.py --show
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402
from common.charts import (  # noqa: E402
    CONFIG_LABELS,
    C_EVENT,
    C_PERIODIC,
    bar_kwargs,
    iqr_error,
    medians,
)

FIGURE = "ablation-quality-a"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")

RC_OVERRIDES = {
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 8.5,
    "xtick.labelsize": 8.0,
    "ytick.labelsize": 8.0,
    "legend.fontsize": 7.5,
    "axes.linewidth": 0.8,
}

HATCH_ACF = "////"
HATCH_BCR = "\\\\\\\\"

#: Throughput annotations sit above the ACF bars, in a darker shade of that
#: paradigm's colour so they read as belonging to the bar beneath them.
TPUT_LABEL_COLOUR = {"event": "#1c3f6e", "periodic": "#a85a16"}

BAR_WIDTH = 0.20


def _annotate_throughput(axis, bars, values, colour, upper_error) -> None:
    """Label bar tops, clearing the bar's own upper error-bar cap.

    Anchoring at the bar height alone would put the text inside the IQR whisker,
    which struck through the numbers on the taller periodic bars.
    """

    for index, (bar, value) in enumerate(zip(bars, values)):
        top = bar.get_height() + float(upper_error[1][index])
        axis.annotate(
            f"{value:.0f}",
            xy=(bar.get_x() + bar.get_width() / 2, top),
            xytext=(0, 2.5), textcoords="offset points",
            ha="center", va="bottom", fontsize=6.5, color=colour,
        )


def _draw(axis, data) -> None:
    labels = [CONFIG_LABELS[name] for name in data["configs"]]
    x = np.arange(len(labels), dtype=float)
    offsets = (-1.5 * BAR_WIDTH, -0.5 * BAR_WIDTH, 0.5 * BAR_WIDTH, 1.5 * BAR_WIDTH)

    # Draw order: ACF-event, BCR-event, ACF-periodic, BCR-periodic.
    plan = (
        ("event", "acf", HATCH_ACF, offsets[0]),
        ("event", "bind_conflict", HATCH_BCR, offsets[1]),
        ("periodic", "acf", HATCH_ACF, offsets[2]),
        ("periodic", "bind_conflict", HATCH_BCR, offsets[3]),
    )
    colour = {"event": C_EVENT, "periodic": C_PERIODIC}

    tops = []
    for paradigm, metric, hatch, offset in plan:
        bands = data["series"][paradigm][metric]
        values = medians(bands)
        error = iqr_error(bands)
        bars = axis.bar(
            x + offset, values, width=BAR_WIDTH, color=colour[paradigm],
            hatch=hatch, yerr=error, **bar_kwargs(),
        )
        if metric == "acf":
            _annotate_throughput(
                axis, bars, data["series"][paradigm]["throughput"],
                TPUT_LABEL_COLOUR[paradigm], error,
            )
        else:
            tops.extend(m + e for m, e in zip(values, error[1]))

    axis.set_xticks(x)
    axis.set_xticklabels(labels, fontsize=7.5)
    axis.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    axis.set_axisbelow(True)
    axis.set_ylim(0, max(0.05, max(tops)) * 1.18)
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.set_ylabel("Conflict rate")


def build_figure(data: dict):
    style.apply(**RC_OVERRIDES)

    figure = plt.figure(figsize=(3.5, 2.1))
    grid = GridSpec(1, 1, figure=figure, left=0.13, right=0.985,
                    top=0.82, bottom=0.18)
    _draw(figure.add_subplot(grid[0, 0]), data)

    handles = [
        mpatches.Patch(facecolor="white", edgecolor="black", linewidth=0.4,
                       hatch=HATCH_ACF, label="ACF"),
        mpatches.Patch(facecolor="white", edgecolor="black", linewidth=0.4,
                       hatch=HATCH_BCR, label="BCR"),
        mpatches.Patch(facecolor=C_EVENT, edgecolor="black", linewidth=0.4,
                       label="Event-driven"),
        mpatches.Patch(facecolor=C_PERIODIC, edgecolor="black", linewidth=0.4,
                       label="Periodic"),
    ]
    figure.legend(
        handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.84), ncol=4,
        frameon=False, fontsize=7.0, columnspacing=0.8, handlelength=1.1,
        handletextpad=0.25,
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
