#!/usr/bin/env python3
"""Render Figure 4, the conflict decomposition over two sweeps.

Label placement follows one rule for the whole 2x2 grid: the column heading names
the swept parameter above the top row, the shared x quantity is named once below
the whole grid, and x tick labels appear only on the bottom row. An earlier
revision repeated the tick labels on both rows and the axis label on both bottom
panels, which is what "be consistent about label placement" was pointing at.

Neither sweep parameter appears as a bare symbol: the column heading carries the
noun and the legend entries are plain values, so the figure needs no $m$ or $W$.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator, PercentFormatter

from common.plotting import (
    COLOR_BLUE,
    COLOR_BLUE_LIGHT,
    COLOR_RED,
    COLOR_RED_LIGHT,
    FONT_PARAMS,
    mean_std,
    save_figure,
)
from common.validation import load_verified
from figures.fig4_conflict.experiment import (
    MANIFEST,
    OUTPUT_DIR,
    SCHEDULER_COUNTS,
    TIER_WIDTHS,
    VERIFIED_CACHE,
    constants,
)

GAPS = (0.5, 1.0, 2.5, 5.0)
BLUE_SCALE = ("#d1e5f0", COLOR_BLUE_LIGHT, "#4393c3", COLOR_BLUE)
RED_SCALE = ("#fddbc7", COLOR_RED_LIGHT, "#d6604d", COLOR_RED)
FIG4_STYLE = {
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 8.5,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8.5,
    "axes.linewidth": 0.8,
    "lines.linewidth": 1.5,
    "lines.markersize": 2.5,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
    **FONT_PARAMS,
}


def _index(data: dict) -> dict[tuple[int, int, int | None], list[dict]]:
    grouped: dict[tuple[int, int, int | None], list[dict]] = defaultdict(list)
    for run in data["runs"]:
        grouped[
            (
                int(run["num_schedulers"]),
                int(run["config"]["num_tiers"]),
                run["config"]["sync_gap_cycles"],
            )
        ].append(run)
    return grouped


def _series(
    grouped: dict[tuple[int, int, int | None], list[dict]],
    schedulers: int,
    tiers: int,
) -> tuple[list[dict], list[dict]]:
    event = grouped[(schedulers, tiers, None)]
    event_by_seed = {
        int(run["seed"]): float(run["summary"]["total_conflict_rate"])
        for run in event
    }
    bind_mean, bind_std, _ = mean_std(event_by_seed.values())
    bind_points: list[dict] = []
    stale_points: list[dict] = []
    for gap in GAPS:
        periodic = grouped[(schedulers, tiers, int(gap / 0.1))]
        periodic_by_seed = {
            int(run["seed"]): float(run["summary"]["total_conflict_rate"])
            for run in periodic
        }
        stale_mean, stale_std, _ = mean_std(
            max(0.0, periodic_by_seed[seed] - event_by_seed[seed])
            for seed in sorted(event_by_seed)
        )
        bind_points.append({"x": gap, "mean": bind_mean, "std": bind_std})
        stale_points.append(
            {"x": gap, "mean": stale_mean, "std": stale_std}
        )
    return bind_points, stale_points


def _draw_bind(axis, series: dict, values: tuple[int, ...], colors,
               *, show_xticklabels: bool) -> None:
    upper: list[float] = []
    for value, color in zip(values, colors):
        points = series[value]
        x = np.asarray([point["x"] for point in points])
        mean = np.asarray([point["mean"] for point in points])
        std = np.asarray([point["std"] for point in points])
        axis.plot(x, mean, color=color, linestyle=":", marker="o", markersize=3.5)
        axis.fill_between(x, mean - std, mean + std, color=color, alpha=0.12)
        upper.extend((mean + std).tolist())
    axis.set_ylim(0, max(upper) * 1.25)
    _format_axis(axis, show_xticklabels=show_xticklabels)


def _draw_stale(axis, series: dict, values: tuple[int, ...], colors,
                *, show_xticklabels: bool) -> None:
    for value, color in zip(values, colors):
        points = series[value]
        x = np.asarray([point["x"] for point in points])
        mean = np.asarray([point["mean"] for point in points])
        std = np.asarray([point["std"] for point in points])
        axis.plot(x, mean, color=color, linestyle="-", marker="s", markersize=3.5)
        axis.fill_between(
            x, np.maximum(0.0, mean - std), mean + std,
            color=color, alpha=0.12,
        )
    axis.set_ylim(bottom=0)
    _format_axis(axis, show_xticklabels=show_xticklabels)


def _format_axis(axis, *, show_xticklabels: bool) -> None:
    """Apply the shared axis format; only the bottom row carries x tick labels."""

    # decimals=0 on every panel: the two rows span different ranges and
    # would otherwise pick different precisions ("15.0%" against "75%").
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    # Pin the tick count: the panels are short enough that the auto locator
    # otherwise drops to two ticks on the narrower ranges.
    axis.yaxis.set_major_locator(MaxNLocator(nbins=4))
    axis.set_xticks(GAPS)
    if show_xticklabels:
        axis.set_xticklabels(["0.5", "1", "2.5", "5"], rotation=30, ha="right")
    else:
        axis.set_xticklabels([])
    axis.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


def _plain(value: int) -> str:
    """Legend value with the paper's thin-space thousands separator."""

    return "$" + f"{value:,}".replace(",", r"\,") + "$"


def build_figure(data: dict):
    grouped = _index(data)
    scheduler = {"bind": {}, "stale": {}}
    width = {"bind": {}, "stale": {}}
    for schedulers in SCHEDULER_COUNTS:
        scheduler["bind"][schedulers], scheduler["stale"][schedulers] = _series(
            grouped, schedulers, 10
        )
    for tier_width in TIER_WIDTHS:
        tiers = constants().num_nodes // tier_width
        width["bind"][tier_width], width["stale"][tier_width] = _series(
            grouped, 10, tiers
        )

    with mpl.rc_context(FIG4_STYLE):
        figure = plt.figure(figsize=(3.5, 3))
        grid = GridSpec(
            3, 2, figure=figure,
            height_ratios=[0.42, 1, 1],
            hspace=0.22, wspace=0.46,
            left=0.14, right=0.86, top=0.98, bottom=0.16,
        )
        legend_axis = figure.add_subplot(grid[0, :])
        legend_axis.axis("off")
        axes = [
            figure.add_subplot(grid[1, 0]),
            figure.add_subplot(grid[1, 1]),
            figure.add_subplot(grid[2, 0]),
            figure.add_subplot(grid[2, 1]),
        ]
        # Top row carries no x tick labels; the bottom row carries them once.
        _draw_bind(axes[0], scheduler["bind"], SCHEDULER_COUNTS, BLUE_SCALE,
                   show_xticklabels=False)
        _draw_bind(axes[1], width["bind"], TIER_WIDTHS, RED_SCALE,
                   show_xticklabels=False)
        _draw_stale(axes[2], scheduler["stale"], SCHEDULER_COUNTS, BLUE_SCALE,
                    show_xticklabels=True)
        _draw_stale(axes[3], width["stale"], TIER_WIDTHS, RED_SCALE,
                    show_xticklabels=True)

        axes[0].set_title("Scheduler count", pad=3)
        axes[1].set_title("Equal-score group size", pad=3)
        axes[0].set_ylabel("Bind race")
        axes[2].set_ylabel("Stale state")
        # Both columns sweep the same quantity, so it is named once for the grid
        # rather than repeated under each bottom panel.
        figure.supxlabel("Sync period $G$ (s)", y=0.005,
                         fontsize=FIG4_STYLE["axes.labelsize"])

        scheduler_handles = [
            Patch(facecolor=color, edgecolor="none", label=str(value))
            for value, color in zip(SCHEDULER_COUNTS, BLUE_SCALE)
        ]
        width_handles = [
            Patch(facecolor=color, edgecolor="none", label=_plain(value))
            for value, color in zip(TIER_WIDTHS, RED_SCALE)
        ]
        first_legend = legend_axis.legend(
            handles=scheduler_handles, loc="upper left",
            bbox_to_anchor=(0, 1.0), ncol=2,
            columnspacing=0.6, handlelength=0.9, handletextpad=0.3,
            labelspacing=0.3, frameon=False, fontsize=7.2,
        )
        legend_axis.add_artist(first_legend)
        legend_axis.legend(
            handles=width_handles, loc="upper right",
            bbox_to_anchor=(1, 1.0), ncol=2,
            columnspacing=0.6, handlelength=0.9, handletextpad=0.3,
            labelspacing=0.3, frameon=False, fontsize=7.2,
        )
    return figure


def main() -> None:
    mpl.rcParams.update(FIG4_STYLE)
    data = load_verified(VERIFIED_CACHE, MANIFEST)
    figure = build_figure(data)
    for path in save_figure(
        figure, OUTPUT_DIR, "conflict-decomposition", pad_inches=0.0
    ):
        print(path)
    plt.close(figure)


if __name__ == "__main__":
    main()
