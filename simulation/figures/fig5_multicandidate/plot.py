#!/usr/bin/env python3
"""Render Figure 5 multi-candidate effectiveness."""

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
    FONT_PARAMS,
    mean_std,
    save_figure,
)
from common.validation import load_verified
from figures.fig5_multicandidate.experiment import OUTPUT_DIR
from figures.fig5_multicandidate.robustness.experiment import (
    MANIFEST as ROBUSTNESS_MANIFEST,
    VERIFIED_CACHE as ROBUSTNESS_CACHE,
    constants,
)

GAPS = (0.5, 1.0, 2.5, 5.0)
# Simulator backup counts, used as data keys. The paper counts the whole
# candidate list, so K = backups + 1; only _k_label() does that conversion.
PLOTTED_BACKUPS = (0, 1, 2, 4)
K_COLORS = (COLOR_GRAY_LIGHT, "#d1e5f0", "#92c5de", COLOR_BLUE)
SETTINGS = ((10, 2_000), (20, 400))
FIG5_STYLE = {
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


def _index(
    data: dict,
) -> dict[tuple[int, int, int, int | None], list[dict]]:
    grouped: dict[
        tuple[int, int, int, int | None], list[dict]
    ] = defaultdict(list)
    for run in data["runs"]:
        width = constants().num_nodes // int(run["config"]["num_tiers"])
        grouped[
            (
                int(run["num_schedulers"]),
                width,
                int(run["config"]["num_backup"]),
                run["config"]["sync_gap_cycles"],
            )
        ].append(run)
    return grouped


def _series(
    grouped: dict[tuple[int, int, int, int | None], list[dict]],
    schedulers: int,
    width: int,
    fallbacks: int,
) -> tuple[list[dict], list[dict]]:
    event = {
        int(run["seed"]): float(run["summary"]["total_conflict_rate"])
        for run in grouped[(schedulers, width, fallbacks, None)]
    }
    bind_mean, bind_std, _ = mean_std(event.values())
    bind_points: list[dict] = []
    stale_points: list[dict] = []
    for gap in GAPS:
        periodic = {
            int(run["seed"]): float(run["summary"]["total_conflict_rate"])
            for run in grouped[
                (schedulers, width, fallbacks, int(gap / 0.1))
            ]
        }
        stale_mean, stale_std, _ = mean_std(
            max(0.0, periodic[seed] - event[seed])
            for seed in sorted(event)
        )
        bind_points.append({"x": gap, "mean": bind_mean, "std": bind_std})
        stale_points.append(
            {"x": gap, "mean": stale_mean, "std": stale_std}
        )
    return bind_points, stale_points


def _format_axis(axis) -> None:
    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    # Pin the tick count; the short panels make the auto locator drop to two.
    axis.yaxis.set_major_locator(MaxNLocator(nbins=4))
    axis.set_xticks(GAPS)
    axis.set_xticklabels(["0.5", "1", "2.5", "5"], rotation=30, ha="right")
    axis.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


def _k_label(backups: int) -> str:
    """Legend label in the paper's candidate-list length, not the backup count."""

    return rf"$K={backups + 1}$"


def _draw_setting(axis, bind: dict, stale: dict) -> float:
    upper: list[float] = []
    for fallbacks, color in zip(PLOTTED_BACKUPS, K_COLORS):
        for points, linestyle, marker in (
            (stale[fallbacks], "-", "s"),
            (bind[fallbacks], ":", "o"),
        ):
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
            )
            axis.fill_between(
                x,
                np.maximum(0.0, mean - std),
                mean + std,
                color=color,
                alpha=0.10,
                linewidth=0,
            )
            upper.extend((mean + std).tolist())
    _format_axis(axis)
    return max(upper)


def _draw_bind_inset(axis, bind: dict) -> None:
    """Magnify the near-zero bind-race band of one panel.

    Under per-decision event synchronization the bind-race floor at the default
    point spans 2.0% down to 0.4%, a fivefold effect of $K$ that is
    indistinguishable from zero on an axis that must also reach the 90% range
    of the stale-state curves. The inset replots exactly the same series on its
    own scale; no data path differs from the parent panel.

    Args:
        axis: Parent panel to attach the inset to.
        bind: Bind-race point lists keyed by backup count, as passed to
            ``_draw_setting``.
    """

    inset = axis.inset_axes([0.40, 0.14, 0.56, 0.38])
    upper: list[float] = []
    for fallbacks, color in zip(PLOTTED_BACKUPS, K_COLORS):
        points = bind[fallbacks]
        x = np.asarray([point["x"] for point in points])
        mean = np.asarray([point["mean"] for point in points])
        inset.plot(x, mean, color=color, linestyle=":", marker="o",
                   markersize=2.0, linewidth=0.9)
        upper.extend(mean.tolist())

    inset.set_ylim(0, max(upper) * 1.15)
    inset.set_xlim(axis.get_xlim())
    inset.set_xticks(GAPS)
    inset.set_xticklabels([])
    inset.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    inset.yaxis.set_major_locator(MaxNLocator(nbins=2))
    inset.tick_params(axis="both", labelsize=5.4, length=1.6, pad=1.0)
    inset.grid(True, linestyle="--", alpha=0.35, linewidth=0.4)
    for spine in inset.spines.values():
        spine.set_linewidth(0.5)
    # A short title only: the inset is half a panel wide, so anything longer
    # overflows its own axes and lands on the parent's curves.
    inset.set_title("Bind-race zoom", fontsize=5.6, pad=1.5)


def build_figure(data: dict):
    grouped = _index(data)
    setting_data = {}
    for schedulers, width in SETTINGS:
        bind: dict[int, list[dict]] = {}
        stale: dict[int, list[dict]] = {}
        for fallbacks in PLOTTED_BACKUPS:
            bind[fallbacks], stale[fallbacks] = _series(
                grouped, schedulers, width, fallbacks
            )
        setting_data[(schedulers, width)] = (bind, stale)

    with mpl.rc_context(FIG5_STYLE):
        figure = plt.figure(figsize=(3.5, 1.85))
        grid = GridSpec(
            2, 2, figure=figure,
            height_ratios=[0.42, 1],
            hspace=0.30, wspace=0.46,
            left=0.14, right=0.86, top=0.98, bottom=0.23,
        )
        legend_axis = figure.add_subplot(grid[0, :])
        legend_axis.axis("off")
        axes = [
            figure.add_subplot(grid[1, 0]),
            figure.add_subplot(grid[1, 1]),
        ]
        upper = []
        for axis, setting in zip(axes, SETTINGS):
            bind, stale = setting_data[setting]
            upper.append(_draw_setting(axis, bind, stale))
        y_max = max(upper) * 1.12
        for axis in axes:
            axis.set_ylim(0, y_max)
        # Only the default point needs the magnifier: at higher contention the
        # bind-race floor already spans 3% to 13% and reads on the main axis.
        _draw_bind_inset(axes[0], setting_data[SETTINGS[0]][0])
        axes[0].set_title("Default point", pad=3)
        axes[1].set_title("Higher contention", pad=3)
        axes[0].set_ylabel("Conflict rate")
        # Both panels sweep the same quantity: name it once for the figure.
        figure.supxlabel("Sync period $G$ (s)", y=0.01,
                         fontsize=FIG5_STYLE["axes.labelsize"])

        color_handles = [
            Patch(facecolor=color, edgecolor="none", label=_k_label(fallbacks))
            for fallbacks, color in zip(PLOTTED_BACKUPS, K_COLORS)
        ]
        color_handles = [
            color_handles[index] for index in (0, 2, 1, 3)
        ]
        first_legend = legend_axis.legend(
            handles=color_handles,
            loc="upper left",
            bbox_to_anchor=(0, 1.0),
            ncol=2,
            columnspacing=0.6,
            handlelength=0.9,
            handletextpad=0.3,
            labelspacing=0.3,
            frameon=False,
            fontsize=7.2,
        )
        legend_axis.add_artist(first_legend)
        type_handles = [
            Line2D(
                [0], [0], color="#444444", linestyle="-",
                marker="s", markersize=3.5, label="Stale-state",
            ),
            Line2D(
                [0], [0], color="#444444", linestyle=":",
                marker="o", markersize=3.5, label="Bind-race",
            ),
        ]
        legend_axis.legend(
            handles=type_handles,
            loc="upper right",
            bbox_to_anchor=(1, 1.0),
            ncol=1,
            handlelength=1.4,
            handletextpad=0.35,
            labelspacing=0.3,
            frameon=False,
            fontsize=7.2,
        )
    return figure


def main() -> None:
    mpl.rcParams.update(FIG5_STYLE)
    data = load_verified(ROBUSTNESS_CACHE, ROBUSTNESS_MANIFEST)
    figure = build_figure(data)
    for path in save_figure(figure, OUTPUT_DIR, "mechanism-study"):
        print(path)
    plt.close(figure)


if __name__ == "__main__":
    main()
