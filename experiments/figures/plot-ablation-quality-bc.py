#!/usr/bin/env python3
"""Figure `ablation-quality-bc`: placement quality per mechanism combination.

Two stacked panels over the same four ablation configurations:

  (a) the mean framework score of the accepted candidate, zoomed around the
      cluster mean with each paradigm's Vanilla run drawn as a dotted reference
      and the change from it printed above the bar;
  (b) how the accepted candidate ranked within its list, as stacked shares.

Panel (b) covers only the two configurations that have a fallback list, because
without one the accepted candidate is always rank 0 and the panel would compare
a bar against nothing. Rank 0 dominates both bars, so its segment is truncated
and the clipped bars are marked at the boundary.

Input
    experiments/work/figure-data/ablation-quality-bc.json, written by

        python ../scripts/export-figure-data.py --figure ablation-quality-bc

    which reads the per-trial `quality.json` records under `results/ablation/`.
    This script holds no measurements of its own.

Usage
    python plot-ablation-quality-bc.py
    python plot-ablation-quality-bc.py --show
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

FIGURE = "ablation-quality-bc"
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

PARADIGMS = (("event", C_EVENT, "#1c3f6e"), ("periodic", C_PERIODIC, "#a85a16"))

#: Rank shades run light to dark within each paradigm's hue.
RANK_COLOURS = {
    "event": ["#dbe6f2", "#7aa6d1", "#285a8f"],
    "periodic": ["#fee5d3", "#fdae6b", "#d95f0e"],
}
#: The legend speaks about ranks in general, so it uses neutral greys rather
#: than either paradigm's ramp.
RANK_LEGEND_COLOURS = ["#eeeeee", "#999999", "#444444"]

#: Configurations that have a fallback list, so an accepted rank above 0 exists.
RANK_CONFIGS = ("multicandidate", "parkour")

BAR_WIDTH = 0.36
RANK_YMAX = 0.30
GRID_KW = {"linestyle": "--", "alpha": 0.45, "linewidth": 0.6}


def _draw_score(axis, data) -> None:
    labels = [CONFIG_LABELS[name] for name in data["configs"]]
    x = np.arange(len(labels), dtype=float)

    drawn = []
    for paradigm, colour, label_colour in PARADIGMS:
        bands = [cell["score"] for cell in data["series"][paradigm]]
        values = medians(bands)
        error = iqr_error(bands)
        offset = -BAR_WIDTH / 2 if paradigm == "event" else BAR_WIDTH / 2
        axis.bar(x + offset, values, width=BAR_WIDTH, yerr=error, color=colour,
                 **bar_kwargs())
        drawn.append((values, error, colour, label_colour, offset))

    axis.set_xticks(x)
    axis.set_xticklabels(labels, fontsize=7.5)
    axis.grid(True, axis="y", **GRID_KW)
    axis.set_axisbelow(True)

    # Differences between configurations are small next to the absolute score,
    # so the axis is zoomed to the band the bars actually occupy.
    present = [value for values, *_ in drawn for value in values if value > 0]
    if present:
        upper = [e for _, error, *_ in drawn for e in error[1]]
        margin = max(max(upper) * 1.5, 2.0)
        axis.set_ylim(min(present) - margin, max(present) + margin)

    for values, _, colour, label_colour, offset in drawn:
        baseline = values[0]
        axis.axhline(baseline, color=colour, linestyle=":", linewidth=0.7,
                     alpha=0.7)
        for index, value in enumerate(values):
            if index == 0 or value == 0 or baseline == 0:
                continue
            axis.annotate(
                f"{(value - baseline) / baseline * 100:+.2f}%",
                xy=(x[index] + offset, value),
                xytext=(-2 if offset < 0 else 2, 1.5),
                textcoords="offset points", ha="center", va="bottom",
                fontsize=6.5, color=label_colour,
            )

    axis.set_ylabel("Mean score")
    axis.set_title("(a) Scheduling quality", loc="left", pad=3)
    axis.text(0.02, 0.97, "bar-top: Δ vs Vanilla\ndotted = Vanilla baseline",
              transform=axis.transAxes, fontsize=7, color="#555555",
              ha="left", va="top", style="italic")


def _draw_ranks(axis, data) -> None:
    indices = [data["configs"].index(name) for name in RANK_CONFIGS]
    x = np.arange(len(indices), dtype=float)

    for paradigm, _, _ in PARADIGMS:
        cells = [data["series"][paradigm][index] for index in indices]
        offset = -BAR_WIDTH / 2 if paradigm == "event" else BAR_WIDTH / 2

        shares = np.array([
            [cell["rank_shares"][str(rank)]["median"] or 0.0 for cell in cells]
            for rank in range(3)
        ])
        # Componentwise medians need not sum to one; renormalize so the stack
        # reads as a composition.
        totals = shares.sum(axis=0)
        shares = np.divide(shares, totals, out=np.zeros_like(shares),
                           where=totals > 0)

        bottom = np.zeros(len(indices), dtype=float)
        for rank in (2, 1, 0):
            bars = axis.bar(x + offset, shares[rank], width=BAR_WIDTH,
                            bottom=bottom, color=RANK_COLOURS[paradigm][rank],
                            edgecolor="black", linewidth=0.4)
            for bar, segment_bottom, value in zip(bars, bottom, shares[rank]):
                if value < 0.009:
                    continue
                if rank == 0:
                    # Rank 0 is clipped, so its label goes inside the part that
                    # stays visible rather than at the segment's true centre.
                    label_y = segment_bottom + max(0.0, RANK_YMAX - segment_bottom) * 0.55
                else:
                    label_y = segment_bottom + value / 2
                axis.annotate(
                    f"{value * 100:.1f}%",
                    xy=(bar.get_x() + bar.get_width() / 2, label_y),
                    ha="center", va="center", fontsize=6.0,
                    color="black" if rank == 0 else "white",
                )
            bottom += shares[rank]

        # Whiskers at the rank-2 boundary and at the whole fallback share.
        # Both are aggregated per trial rather than by summing rank-wise IQRs.
        for bands in (
            [cell["rank_shares"]["2"] for cell in cells],
            [cell["rank_gt0"] for cell in cells],
        ):
            axis.errorbar(
                x + offset, medians(bands), yerr=iqr_error(bands),
                fmt="none", ecolor="#333333", elinewidth=0.7, capsize=2.0,
            )

    # Mark every bar as clipped at the truncation boundary.
    for position in np.concatenate([x - BAR_WIDTH / 2, x + BAR_WIDTH / 2]):
        for start, end in ((-0.28, -0.05), (0.05, 0.28)):
            axis.plot(
                [position + BAR_WIDTH * start, position + BAR_WIDTH * end],
                [RANK_YMAX - 0.008, RANK_YMAX + 0.008],
                color="black", linewidth=0.7, clip_on=False,
            )

    axis.set_ylim(0, RANK_YMAX)
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    axis.set_xticks(x)
    axis.set_xticklabels([CONFIG_LABELS[name] for name in RANK_CONFIGS],
                         fontsize=7.5)
    axis.grid(True, axis="y", **GRID_KW)
    axis.set_axisbelow(True)
    axis.set_ylabel("Successful bindings\n(rank 0 truncated)")
    axis.set_title("(b) Accepted-rank composition", loc="left", pad=3)


def build_figure(data: dict):
    style.apply(**RC_OVERRIDES)

    figure = plt.figure(figsize=(3.5, 4.2))
    grid = GridSpec(2, 1, figure=figure, hspace=0.45, left=0.16, right=0.985,
                    top=0.84, bottom=0.10)
    _draw_score(figure.add_subplot(grid[0, 0]), data)
    _draw_ranks(figure.add_subplot(grid[1, 0]), data)

    handles = [
        mpatches.Patch(facecolor=C_EVENT, edgecolor="black", linewidth=0.4,
                       label="Event-driven"),
        mpatches.Patch(facecolor=C_PERIODIC, edgecolor="black", linewidth=0.4,
                       label="Periodic"),
    ]
    handles += [
        mpatches.Patch(facecolor=RANK_LEGEND_COLOURS[rank], edgecolor="black",
                       linewidth=0.4, label=f"Rank {rank}")
        for rank in (2, 1, 0)
    ]
    figure.legend(
        handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.86), ncol=5,
        frameon=False, fontsize=7.0, columnspacing=0.6, handlelength=1.1,
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
