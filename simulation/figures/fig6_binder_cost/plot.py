#!/usr/bin/env python3
"""Render Figure 6 fixed and variable candidate-list cost trade-offs."""

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
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator, PercentFormatter

from common.plotting import (
    COLOR_BLUE,
    COLOR_GRAY_LIGHT,
    COLOR_RED,
    FONT_PARAMS,
    mean_std,
    save_figure,
)
from common.validation import load_verified
from figures.fig6_binder_cost.experiment import (
    MANIFEST,
    OUTPUT_DIR,
    SETTINGS,
    VERIFIED_CACHE,
    constants,
)

GAPS = (0.0, 1.0, 2.5, 5.0)
PAPER_SETTING = (10, 2_000)
PAPER_OUTPUT_STEM = "binder-cost-comparison"
DESIGNS = ("k0", "k8", "threshold")
CONFLICT_COLORS = {
    "k0": COLOR_GRAY_LIGHT,
    "k8": COLOR_BLUE,
    "threshold": COLOR_RED,
}
# Keys are simulator backup counts; the paper counts the whole candidate list,
# so k0 is K=1 (native single-target) and k8 is K=9.
DESIGN_LABELS = {
    "k0": "$K=1$",
    "k8": "$K=9$",
    "threshold": r"$\epsilon=0.1$",
}
FIG6_STYLE = {
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


def _index(data: dict) -> dict[tuple[int, int, str, int | None], list[dict]]:
    """Group seed runs by objective setting, list design, and update gap."""

    grouped: dict[tuple[int, int, str, int | None], list[dict]] = defaultdict(list)
    for run in data["runs"]:
        width = constants().num_nodes // int(run["config"]["num_tiers"])
        design = (
            "threshold"
            if run["config"]["list_mode"] == "threshold"
            else f"k{int(run['config']['num_backup'])}"
        )
        grouped[
            (
                int(run["num_schedulers"]),
                width,
                design,
                run["config"]["sync_gap_cycles"],
            )
        ].append(run)
    return grouped


def _points(
    grouped: dict[tuple[int, int, str, int | None], list[dict]],
    schedulers: int,
    width: int,
    design: str,
    metric: str,
) -> list[dict]:
    """Aggregate a seed-wise summary metric over all requested sync gaps."""

    points: list[dict] = []
    for gap in GAPS:
        gap_cycles = None if gap == 0.0 else int(gap / constants().cycle_seconds)
        mean, std, _ = mean_std(
            float(run["summary"][metric])
            for run in grouped[(schedulers, width, design, gap_cycles)]
        )
        points.append({"x": gap, "mean": mean, "std": std})
    return points


def _format_axis(axis, *, percent: bool = False) -> None:
    """Apply Figure 4/5-compatible axes without duplicating their panel wording."""

    axis.set_xticks(GAPS)
    axis.set_xticklabels(["0", "1", "2.5", "5"], rotation=30, ha="right")
    axis.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    axis.set_axisbelow(True)
    if percent:
        axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        # Pin the tick count; the short panel makes the auto locator drop to
        # two. Only the linear axis: the right panel is log-scaled.
        axis.yaxis.set_major_locator(MaxNLocator(nbins=4))


def _plot_series(axis, points: list[dict], *, color: str, linestyle: str, marker: str) -> None:
    """Plot a mean line and seed-level standard-deviation band."""

    x = np.asarray([point["x"] for point in points])
    mean = np.asarray([point["mean"] for point in points])
    std = np.asarray([point["std"] for point in points])
    axis.plot(x, mean, color=color, linestyle=linestyle, marker=marker, markersize=3.5)
    axis.fill_between(
        x,
        np.maximum(0.0, mean - std),
        mean + std,
        color=color,
        alpha=0.10,
        linewidth=0,
    )


def build_figure(data: dict, setting: tuple[int, int]):
    """Build one compact two-panel Figure 6 for a fixed ``(m, W)`` setting."""

    schedulers, width = setting
    if setting not in SETTINGS:
        raise ValueError(f"unsupported Figure 6 setting: {setting}")
    grouped = _index(data)
    conflict = {
        design: _points(
            grouped, schedulers, width, design, "total_conflict_rate"
        )
        for design in DESIGNS
    }
    mean_checks = {
        design: _points(
            grouped, schedulers, width, design, "binder_arbitration_mean_per_pod"
        )
        for design in ("k8", "threshold")
    }
    worst_checks = {
        design: _points(
            grouped, schedulers, width, design, "binder_arbitration_max_per_pod"
        )
        for design in ("k8", "threshold")
    }

    with mpl.rc_context(FIG6_STYLE):
        figure = plt.figure(figsize=(3.5, 1.8))
        grid = GridSpec(
            2,
            2,
            figure=figure,
            height_ratios=[0.42, 1],
            hspace=0.20,
            wspace=0.52,
            left=0.14,
            right=0.86,
            top=0.98,
            bottom=0.24,
        )
        legend_axis = figure.add_subplot(grid[0, :])
        legend_axis.axis("off")
        left = figure.add_subplot(grid[1, 0])
        right = figure.add_subplot(grid[1, 1])

        for design in DESIGNS:
            _plot_series(
                left,
                conflict[design],
                color=CONFLICT_COLORS[design],
                linestyle="-",
                marker="o",
            )
        _format_axis(left, percent=True)
        left.set_ylim(bottom=0)
        # The panel titles duplicated the y labels, so only the y labels remain.
        left.set_ylabel("Conflict rate")

        for design in ("k8", "threshold"):
            color = CONFLICT_COLORS[design]
            _plot_series(
                right,
                mean_checks[design],
                color=color,
                linestyle="-",
                marker="s",
            )
            _plot_series(
                right,
                worst_checks[design],
                color=color,
                linestyle=":",
                marker="o",
            )
        _format_axis(right)
        right.set_yscale("log")
        right.set_ylabel("Checks per pod")
        # Both panels sweep the same quantity: name it once for the figure.
        figure.supxlabel("Sync period $G$ (s)", y=0.01,
                         fontsize=FIG6_STYLE["axes.labelsize"])

        design_handles = [
            Patch(
                facecolor=CONFLICT_COLORS[design],
                edgecolor="none",
                label=DESIGN_LABELS[design],
            )
            for design in DESIGNS
        ]
        first_legend = legend_axis.legend(
            handles=design_handles,
            loc="upper left",
            bbox_to_anchor=(0, 1.0),
            ncol=3,
            columnspacing=0.55,
            handlelength=0.9,
            handletextpad=0.3,
            labelspacing=0.3,
            frameon=False,
            fontsize=7.2,
        )
        legend_axis.add_artist(first_legend)
        type_handles = [
            Line2D(
                [0],
                [0],
                color="#444444",
                linestyle="-",
                marker="s",
                markersize=3.5,
                label="Mean",
            ),
            Line2D(
                [0],
                [0],
                color="#444444",
                linestyle=":",
                marker="o",
                markersize=3.5,
                label="Worst",
            ),
        ]
        legend_axis.legend(
            handles=type_handles,
            loc="upper right",
            bbox_to_anchor=(1, 1.0),
            ncol=2,
            columnspacing=0.55,
            handlelength=1.4,
            handletextpad=0.35,
            labelspacing=0.3,
            frameon=False,
            fontsize=7.2,
        )
    return figure


def main() -> None:
    """Render the selected Figure 6 paper artifact."""

    mpl.rcParams.update(FIG6_STYLE)
    data = load_verified(VERIFIED_CACHE, MANIFEST)
    figure = build_figure(data, PAPER_SETTING)
    for path in save_figure(figure, OUTPUT_DIR, PAPER_OUTPUT_STEM):
        print(path)
    plt.close(figure)


if __name__ == "__main__":
    main()
