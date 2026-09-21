#!/usr/bin/env python3
"""Render Figure 8 total conflict rate by penalty information scope."""

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
    COLOR_GRAY_LIGHT,
    COLOR_RED,
    FONT_PARAMS,
    mean_std,
    save_figure,
)
from common.validation import load_verified
from figures.fig8_penalty_scope.experiment import MANIFEST, OUTPUT_DIR, VERIFIED_CACHE, constants

GAPS = (0.0, 1.0, 2.5, 5.0)
# The non-zero weight is the deployed feedback rate; the comparison here is
# between information scopes at a fixed rate, not across rates.
METHODS = (
    ("baseline", 0.0, "shared", COLOR_GRAY_LIGHT, "$w=0$", "--", None, 3),
    ("shared", 0.5, "shared", COLOR_BLUE, "$w=0.5$ (shared)", "-", "o", 2),
    ("local", 0.5, "local", COLOR_RED, "$w=0.5$ (local)", "-", "o", 1),
)
FIG8_STYLE = {
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


def _index(data: dict) -> dict[tuple[float, str, int | None], list[dict]]:
    """Group runs by penalty weight, information scope, and sync gap."""

    grouped: dict[tuple[float, str, int | None], list[dict]] = defaultdict(list)
    for run in data["runs"]:
        config = run["config"]
        grouped[
            (
                float(config["penalty_weight"]),
                str(config["penalty_scope"]),
                config["sync_gap_cycles"],
            )
        ].append(run)
    return grouped


def _series(
    grouped: dict[tuple[float, str, int | None], list[dict]],
    weight: float,
    scope: str,
) -> list[dict]:
    """Aggregate total conflict rate for one method across sync gaps."""

    points: list[dict] = []
    for gap in GAPS:
        gap_cycles = None if gap == 0.0 else int(gap / constants().cycle_seconds)
        mean, std, _ = mean_std(
            float(run["summary"]["total_conflict_rate"])
            for run in grouped[(weight, scope, gap_cycles)]
        )
        points.append({"x": gap, "mean": mean, "std": std})
    return points


def draw_panel(axis, data: dict, *, show_xlabel: bool = True) -> None:
    """Draw the penalty-scope comparison on an existing axis."""

    grouped = _index(data)
    upper: list[float] = []
    for _, weight, scope, color, _, linestyle, marker, zorder in METHODS:
        points = _series(grouped, weight, scope)
        x = np.asarray([point["x"] for point in points])
        mean = np.asarray([point["mean"] for point in points])
        std = np.asarray([point["std"] for point in points])
        axis.plot(
            x,
            mean,
            color=color,
            linestyle=linestyle,
            marker=marker,
            markersize=3.5,
            zorder=zorder,
        )
        axis.fill_between(
            x,
            np.maximum(0.0, mean - std),
            mean + std,
            color=color,
            alpha=0.10,
            linewidth=0,
            zorder=zorder - 0.1,
        )
        upper.extend((mean + std).tolist())

    axis.set_xticks(GAPS)
    axis.set_xticklabels(["0", "1", "2.5", "5"], rotation=30, ha="right")
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    # Pin the tick count; the short panels make the auto locator drop to two.
    axis.yaxis.set_major_locator(MaxNLocator(nbins=4))
    axis.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    axis.set_axisbelow(True)
    axis.set_ylim(0, max(upper) * 1.12)
    # Suppressed when composited into Figure 7, which names the shared x
    # quantity once for all three panels.
    if show_xlabel:
        axis.set_xlabel("Sync period $G$ (s)")
    axis.set_ylabel("Conflict rate")
    axis.set_title("Shared vs. per-scheduler feedback", pad=3)


def draw_legend(axis) -> None:
    """Draw the penalty-scope legend on an existing legend axis."""

    axis.axis("off")
    axis.legend(
        handles=[
            Patch(facecolor=color, edgecolor="none", label=label)
            for _, _, _, color, label, _, _, _ in METHODS
        ],
        loc="upper center",
        bbox_to_anchor=(0.5, 1.0),
        ncol=3,
        columnspacing=0.55,
        handlelength=0.9,
        handletextpad=0.3,
        labelspacing=0.3,
        frameon=False,
        fontsize=7.2,
    )


def build_figure(data: dict):
    """Build the compact single-panel Figure 8 paper artifact."""

    with mpl.rc_context(FIG8_STYLE):
        figure = plt.figure(figsize=(3.5, 1.65))
        grid = GridSpec(
            2,
            1,
            figure=figure,
            height_ratios=[0.42, 1],
            hspace=0.16,
            left=0.14,
            right=0.86,
            top=0.98,
            bottom=0.15,
        )
        legend_axis = figure.add_subplot(grid[0])
        axis = figure.add_subplot(grid[1])
        draw_legend(legend_axis)
        draw_panel(axis, data)
    return figure


def main() -> None:
    """Render Figure 8 only from its hash-locked verified cache."""

    mpl.rcParams.update(FIG8_STYLE)
    data = load_verified(VERIFIED_CACHE, MANIFEST)
    figure = build_figure(data)
    for path in save_figure(figure, OUTPUT_DIR, "penalty-scope"):
        print(path)
    plt.close(figure)


if __name__ == "__main__":
    main()
