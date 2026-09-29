#!/usr/bin/env python3
"""Render Figure 7 penalty-only and multi-candidate synergy effects."""

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
    COLOR_BLUE_LIGHT,
    COLOR_GRAY_LIGHT,
    COLOR_RED,
    COLOR_RED_LIGHT,
    FONT_PARAMS,
    mean_std,
    save_figure,
)
from common.validation import load_verified
from figures.fig7_penalty.experiment import MANIFEST, OUTPUT_DIR, VERIFIED_CACHE, constants
from figures.fig8_penalty_scope.experiment import (
    MANIFEST as SCOPE_MANIFEST,
    VERIFIED_CACHE as SCOPE_VERIFIED_CACHE,
)
from figures.fig8_penalty_scope.panels import draw_legend as draw_scope_legend
from figures.fig8_penalty_scope.panels import draw_panel as draw_scope_panel

GAPS = (0.0, 1.0, 2.5, 5.0)
PENALTY_ONLY = ((0, 0.0), (0, 0.3), (0, 0.5), (0, 0.7))
# Centre panel plots the deployed feedback rate. The first element of each
# pair is the simulator backup count, so K = element + 1.
SYNERGY = ((0, 0.0), (0, 0.5), (2, 0.0), (2, 0.5))
BLUE_SCALE = (COLOR_GRAY_LIGHT, COLOR_BLUE_LIGHT, "#4393c3", COLOR_BLUE)
RED_SCALE = (COLOR_GRAY_LIGHT, COLOR_RED_LIGHT, "#d6604d", COLOR_RED)
FIG7_STYLE = {
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


def _index(data: dict) -> dict[tuple[int, float, int | None], list[dict]]:
    """Group runs by candidate budget, penalty weight, and sync gap."""

    grouped: dict[tuple[int, float, int | None], list[dict]] = defaultdict(list)
    for run in data["runs"]:
        config = run["config"]
        grouped[
            (
                int(config["num_backup"]),
                float(config["penalty_weight"]),
                config["sync_gap_cycles"],
            )
        ].append(run)
    return grouped


def _series(
    grouped: dict[tuple[int, float, int | None], list[dict]],
    num_backup: int,
    weight: float,
) -> tuple[list[dict], list[dict]]:
    """Return paired bind-race and stale-state conflict proxies for one setting."""

    event = {
        int(run["seed"]): float(run["summary"]["total_conflict_rate"])
        for run in grouped[(num_backup, weight, None)]
    }
    bind_mean, bind_std, _ = mean_std(event.values())
    bind_points: list[dict] = []
    stale_points: list[dict] = []
    for gap in GAPS:
        if gap == 0.0:
            periodic = event
        else:
            periodic = {
                int(run["seed"]): float(run["summary"]["total_conflict_rate"])
                for run in grouped[
                    (
                        num_backup,
                        weight,
                        int(gap / constants().cycle_seconds),
                    )
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
    """Apply the compact Figure 4--6 axis treatment."""

    axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    # Pin the tick count; the short panels make the auto locator drop to two.
    axis.yaxis.set_major_locator(MaxNLocator(nbins=4))
    axis.set_xticks(GAPS)
    axis.set_xticklabels(["0", "1", "2.5", "5"], rotation=30, ha="right")
    axis.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    axis.set_axisbelow(True)


def _plot_series(
    axis,
    points: list[dict],
    *,
    color: str,
    linestyle: str,
    marker: str,
) -> float:
    """Draw one mean/std conflict series and return its upper extent."""

    x = np.asarray([point["x"] for point in points])
    mean = np.asarray([point["mean"] for point in points])
    std = np.asarray([point["std"] for point in points])
    axis.plot(x, mean, color=color, linestyle=linestyle, marker=marker, markersize=3.5)
    axis.fill_between(
        x,
        np.maximum(0.0, mean - std),
        mean + std,
        color=color,
        alpha=0.07,
        linewidth=0,
    )
    return float(np.max(mean + std))


def _annotate_bind_floor(
    axis,
    grouped: dict[tuple[int, float, int | None], list[dict]],
    settings: tuple[tuple[int, float], ...],
    placements: tuple[tuple[str, float, float], ...],
) -> None:
    """Label each distinct bind-race floor with its value.

    The floor sits at or below 2% of attempts, which an axis that must also
    reach 90% cannot separate from zero. A magnified inset would not help here:
    every penalty weight produces bit-identical bind-race rates, so the curves
    are exactly superimposed and stay so at any magnification. Naming the value
    states what a zoom could not, namely that the floor is set by the candidate
    list length alone and that no feedback rate moves it.

    Args:
        axis: Panel to annotate.
        grouped: Indexed run cache, as returned by ``_index``.
        settings: The (backup count, feedback rate) pairs drawn in this panel.
        placements: One ``(label, x, y)`` per distinct floor, in the order the
            floors occur from highest to lowest; ``x`` and ``y`` are axes
            fractions for the text.

    Raises:
        AssertionError: The panel holds a different number of distinct floors
            than ``placements`` supplies, which means the data moved and the
            hand-placed labels are no longer positioned against anything.
    """

    levels: dict[float, None] = {}
    for num_backup, weight in settings:
        bind, _ = _series(grouped, num_backup, weight)
        levels.setdefault(round(float(bind[0]["mean"]), 9), None)
    ordered = sorted(levels, reverse=True)
    assert len(ordered) == len(placements), (
        f"panel has {len(ordered)} bind-race floor(s) but {len(placements)} "
        "label placement(s) were supplied"
    )

    for value, (label, text_x, text_y) in zip(ordered, placements):
        axis.annotate(
            label,
            xy=(GAPS[-1], value),
            xycoords="data",
            xytext=(text_x, text_y),
            textcoords="axes fraction",
            fontsize=6.0,
            color="#333333",
            ha="right",
            va="bottom",
            arrowprops={
                "arrowstyle": "-",
                "linewidth": 0.5,
                "color": "#777777",
                "shrinkA": 1.0,
                "shrinkB": 1.5,
            },
        )


def _draw_bind_inset(
    axis,
    grouped: dict[tuple[int, float, int | None], list[dict]],
    settings: tuple[tuple[int, float], ...],
    colors: tuple[str, ...],
) -> None:
    """Magnify the bind-race band where a panel holds more than one floor.

    Two floors that differ by a factor of three still land within 2% of each
    other, so on the main axis they overlap and a leader line cannot say which
    label belongs to which. Replotting them on their own scale separates them,
    and the feedback-rate variants remain superimposed inside the inset, which
    is itself the result: the candidate list sets the floor and the penalty
    does not move it.

    Args:
        axis: Parent panel to attach the inset to.
        grouped: Indexed run cache, as returned by ``_index``.
        settings: The (backup count, feedback rate) pairs drawn in this panel.
        colors: Matching colours, in the same order as ``settings``.
    """

    inset = axis.inset_axes([0.46, 0.15, 0.51, 0.36])
    upper: list[float] = []
    for (num_backup, weight), color in zip(settings, colors):
        bind, _ = _series(grouped, num_backup, weight)
        x = np.asarray([point["x"] for point in bind])
        mean = np.asarray([point["mean"] for point in bind])
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
    inset.set_title("Bind-race zoom", fontsize=5.6, pad=1.5)


def _draw_panel(
    axis,
    grouped: dict[tuple[int, float, int | None], list[dict]],
    settings: tuple[tuple[int, float], ...],
    colors: tuple[str, ...],
) -> float:
    """Draw all candidate/penalty settings in one panel."""

    upper = 0.0
    for (num_backup, weight), color in zip(settings, colors):
        bind, stale = _series(grouped, num_backup, weight)
        upper = max(
            upper,
            _plot_series(
                axis, stale, color=color, linestyle="-", marker="s"
            ),
            _plot_series(
                axis, bind, color=color, linestyle=":", marker="o"
            ),
        )
    _format_axis(axis)
    return upper


def _legend(
    axis,
    settings: tuple[tuple[int, float], ...],
    colors: tuple[str, ...],
    *,
    synergy: bool,
) -> None:
    """Place a compact two-column setting legend above its data panel."""

    labels = (
        [rf"$w={weight:g}$" for _, weight in settings]
        if not synergy
        else [rf"$K={budget + 1},\,w={weight:g}$" for budget, weight in settings]
    )
    handles = [
        Patch(facecolor=color, edgecolor="none", label=label)
        for color, label in zip(colors, labels)
    ]
    axis.legend(
        handles=handles,
        loc="upper left",
        bbox_to_anchor=(0, 1.0),
        ncol=2,
        columnspacing=0.45,
        handlelength=0.85,
        handletextpad=0.25,
        labelspacing=0.3,
        frameon=False,
        fontsize=6.6,
    )


def build_figure(data: dict, scope_data: dict):
    """Build the merged three-panel penalty study paper artifact."""

    grouped = _index(data)
    with mpl.rc_context(FIG7_STYLE):
        figure = plt.figure(figsize=(7.0, 1.9))
        grid = GridSpec(
            3,
            3,
            figure=figure,
            height_ratios=[0.30, 0.30, 1],
            width_ratios=[2, 3, 3],
            hspace=0.05,
            wspace=0.42,
            left=0.07,
            right=0.985,
            top=0.98,
            bottom=0.24,
        )
        setting_legends = [
            figure.add_subplot(grid[0, 0]),
            figure.add_subplot(grid[0, 1]),
        ]
        type_axis = figure.add_subplot(grid[1, 0:2])
        scope_legend = figure.add_subplot(grid[0:2, 2])
        for axis in (*setting_legends, type_axis):
            axis.axis("off")
        axes = [
            figure.add_subplot(grid[2, 0]),
            figure.add_subplot(grid[2, 1]),
            figure.add_subplot(grid[2, 2]),
        ]

        _legend(
            setting_legends[0], PENALTY_ONLY, BLUE_SCALE, synergy=False
        )
        _legend(setting_legends[1], SYNERGY, RED_SCALE, synergy=True)
        type_axis.legend(
            handles=[
                Line2D(
                    [0], [0], color="#444444", linestyle="-", marker="s",
                    markersize=3.5, label="Stale-state",
                ),
                Line2D(
                    [0], [0], color="#444444", linestyle=":", marker="o",
                    markersize=3.5, label="Bind-race",
                ),
            ],
            loc="upper center",
            # Shifted left of true center (0.5): at 0.5 "Bind-race" collides
            # with the "Fallback + penalty" panel title below it.
            bbox_to_anchor=(0.43, 1.0),
            ncol=2,
            columnspacing=0.65,
            handlelength=1.4,
            handletextpad=0.3,
            frameon=False,
            fontsize=7.0,
        )

        upper = [
            _draw_panel(axes[0], grouped, PENALTY_ONLY, BLUE_SCALE),
            _draw_panel(axes[1], grouped, SYNERGY, RED_SCALE),
        ]
        y_max = max(upper) * 1.12
        for axis in axes:
            axis.set_ylim(0, y_max)
        # Name the bind-race floors the compressed axis hides. Text sits in the
        # empty band under the stale-state curves at the long periods.
        _annotate_bind_floor(
            axes[0], grouped, PENALTY_ONLY,
            (("2.0%, every $w$", 0.97, 0.16),),
        )
        # The centre panel holds two floors, which no single leader line can
        # disambiguate on the compressed axis, so it gets the magnifier.
        _draw_bind_inset(axes[1], grouped, SYNERGY, RED_SCALE)
        axes[0].set_title("Penalty only", pad=3)
        axes[1].set_title("Fallback + penalty", pad=3)
        axes[0].set_ylabel("Conflict rate")
        draw_scope_legend(scope_legend)
        draw_scope_panel(axes[2], scope_data, show_xlabel=False)
        # All three panels sweep the same quantity: name it once.
        figure.supxlabel("Sync period $G$ (s)", y=0.01,
                         fontsize=FIG7_STYLE["axes.labelsize"])
    return figure


def main() -> None:
    """Render the merged penalty study from both hash-locked caches."""

    mpl.rcParams.update(FIG7_STYLE)
    data = load_verified(VERIFIED_CACHE, MANIFEST)
    scope_data = load_verified(SCOPE_VERIFIED_CACHE, SCOPE_MANIFEST)
    figure = build_figure(data, scope_data)
    for path in save_figure(figure, OUTPUT_DIR, "penalty-study"):
        print(path)
    plt.close(figure)


if __name__ == "__main__":
    main()
