#!/usr/bin/env python3
"""Render Figure 3, the naive-parallelism motivation panel.

Panel (a) is a grouped stacked bar chart of the conflict decomposition and panel
(b) a line chart against an ideal reference. The encoding is strictly two-factor:
**colour carries the system** (red vanilla, green ParKour, grey ideal) and **line
style plus bar hatch carry the synchronization paradigm** (solid/plain event-driven,
dashed/dotted periodic). Panel (b) therefore holds five lines --- two systems under
two paradigms, plus the ideal reference --- and a single figure-level legend names
both factors so every line is accounted for.

An earlier revision shaded each system twice (one colour per paradigm) while the
legend still fused those shades into one composite handle per system, which left
five drawn lines against three labels with the pairing rule unstated. One colour
per system restores the documented encoding.

Panel (b) plots effective parallelism $m(1-c)$ rather than raw throughput. This
workload injects at a fixed 1000 pods/s, which every arm with five or more
schedulers already saturates, so a throughput axis would collapse into
overlapping flat lines; $m(1-c)$ is the quantity the cost model of Section 3.1
defines and stays informative across the whole sweep.

Style/data separation is preserved: STYLE and RC_PARAMS hold visual constants,
``load_*`` return numpy arrays from the hash-locked verified cache, and
``draw_*`` take pre-loaded arrays.
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
import seaborn as sns
from matplotlib.gridspec import GridSpec
from matplotlib.legend_handler import HandlerTuple
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import PercentFormatter

from common.plotting import FONT_PARAMS, save_figure
from common.validation import load_verified
from figures.fig3_motivation.experiment import (
    MANIFEST,
    OUTPUT_DIR,
    SCHEDULER_COUNTS,
    VERIFIED_CACHE,
    constants,
)

# ============================================================================
#  STYLE — central visual constants, inherited from the original Figure 3.
#    gray  = ideal-no-conflict reference
#    red   = parallel-scheduling baseline
#    green = ParKour (proposed)
#    blue  = conflict classes in the decomposition panel
#    solid / plain patch = event-driven; dashed / dotted patch = periodic
# ============================================================================

_rdbu = sns.color_palette("RdBu", 11)
_greens = sns.color_palette("Greens", 6)

STYLE = {
    "color_ideal": "#888888",
    "color_single": "#555555",
    # One colour per system: the paradigm is carried by line style, never by a
    # second shade, so the legend can name the two factors independently.
    "color_baseline": _rdbu[1],
    "color_parkour": _greens[4],
    "color_stale": _rdbu[-3],
    "color_bind": _rdbu[-1],
    "ls_event": "-",
    # An explicit long dash reads at 6.5 pt where matplotlib's default "--"
    # degenerates into a few indistinct ticks inside a legend handle.
    "ls_periodic": (0, (3.5, 1.4)),
    # Looser dots than the default ":" so the ideal reference is not mistaken
    # for the dotted bar hatch that marks periodic bars in panel (a).
    "ls_ideal": (0, (1, 1.6)),
    "hatch_event": "",
    "hatch_periodic": "....",
    "marker_event": "o",
    "marker_periodic": "s",
    "lw_main": 1.5,
    "lw_ideal": 1.5,
    "bar_edge": "white",
    "alpha_band": 0.18,
}

RC_PARAMS = {
    "font.size": 7.0,
    "axes.labelsize": 7.0,
    "axes.titlesize": 6.8,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.5,
    "legend.title_fontsize": 6.8,
    "axes.linewidth": 0.7,
    "lines.linewidth": 1.2,
    "lines.markersize": 2.5,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
    **FONT_PARAMS,
}

FIG_SIZE = (3.386, 1.87)
LAYOUT = {"left": 0.125, "right": 0.995, "top": 0.755, "bottom": 0.164,
          "wspace": 0.45}
WIDTH_RATIOS = [1, 1]

# The periodic arm both panels describe. One gap keeps the decomposition story
# and the parallelism story on the same experimental conditions, and 1 s is the
# gap the cluster deployment runs at.
PERIODIC_GAP_CYCLES = 10
# ParKour is the deployed configuration. This is the simulator's backup count, so
# it is one less than the paper's candidate-list length: 2 backups == K=3.
PARKOUR_BACKUP = 2
PARKOUR_WEIGHT = 0.5


# ============================================================================
#  Data loaders — pure: read the verified cache, return numpy arrays.
# ============================================================================

def _index(data: dict) -> dict[tuple[int, int | None, int, float], float]:
    """Mean total conflict rate per (schedulers, gap, budget, weight)."""

    grouped: dict[tuple[int, int | None, int, float], list[float]] = defaultdict(list)
    for run in data["runs"]:
        config = run["config"]
        grouped[
            (
                int(run["num_schedulers"]),
                config["sync_gap_cycles"],
                int(config["num_backup"]),
                float(config["penalty_weight"]),
            )
        ].append(float(run["summary"]["total_conflict_rate"]))
    return {key: float(np.mean(values)) for key, values in grouped.items()}


def load_middle(rates: dict) -> dict[str, np.ndarray]:
    """Conflict-rate decomposition per scheduler count, vanilla only.

    Under event-driven synchronization every conflict is a bind race; under
    periodic synchronization the same floor persists plus a stale-state
    component, so stale = max(0, periodic - event) and bind = min(event,
    periodic). This is the cross-arm decomposition of Section 3.2, not a
    per-event label the simulator records.
    """

    counts = np.asarray(SCHEDULER_COUNTS)
    event = np.asarray([rates[(m, None, 0, 0.0)] for m in SCHEDULER_COUNTS])
    periodic = np.asarray(
        [rates[(m, PERIODIC_GAP_CYCLES, 0, 0.0)] for m in SCHEDULER_COUNTS]
    )
    return {
        "sched_counts": counts,
        "event_commit": event,
        "periodic_commit": np.minimum(event, periodic),
        "periodic_stale": np.maximum(0.0, periodic - event),
    }


def load_right(rates: dict) -> dict[str, np.ndarray]:
    """Effective parallelism per scheduler count for both systems and paradigms.

    Effective parallelism is $m(1-c)$, the number of schedulers whose work
    survives; the ideal-no-conflict reference is therefore $m$ itself.
    """

    counts = np.asarray(SCHEDULER_COUNTS, dtype=float)
    out: dict[str, np.ndarray] = {"sched_counts": counts, "ideal": counts.copy()}
    systems = (("base", 0, 0.0), ("parkour", PARKOUR_BACKUP, PARKOUR_WEIGHT))
    paradigms = (("event", None), ("periodic", PERIODIC_GAP_CYCLES))
    for system, backup, weight in systems:
        for paradigm, gap in paradigms:
            out[f"{system}_{paradigm}"] = np.asarray(
                [m * (1.0 - rates[(m, gap, backup, weight)]) for m in SCHEDULER_COUNTS]
            )
    return out


# ============================================================================
#  Drawers — pure: take pre-loaded arrays + STYLE, render onto an axis.
# ============================================================================

def draw_middle(axis, data: dict) -> None:
    """Grouped stacked bars: per scheduler count, event vs periodic side by side."""

    counts = data["sched_counts"]
    x = np.arange(len(counts))
    width = 0.36

    axis.bar(x - width / 2, data["event_commit"], width=width,
             color=STYLE["color_bind"], hatch=STYLE["hatch_event"],
             edgecolor=STYLE["bar_edge"], linewidth=0.6)
    axis.bar(x + width / 2, data["periodic_commit"], width=width,
             color=STYLE["color_bind"], hatch=STYLE["hatch_periodic"],
             edgecolor=STYLE["bar_edge"], linewidth=0.6)
    axis.bar(x + width / 2, data["periodic_stale"], width=width,
             bottom=data["periodic_commit"],
             color=STYLE["color_stale"], hatch=STYLE["hatch_periodic"],
             edgecolor=STYLE["bar_edge"], linewidth=0.6)

    axis.set_xticks(x)
    axis.set_xticklabels([str(int(value)) for value in counts])
    axis.set_xlabel("Number of schedulers")
    axis.set_ylabel("Conflict rate")
    axis.yaxis.set_major_formatter(PercentFormatter(1.0))
    axis.set_yticks([0.0, 0.25, 0.5, 0.75, 1.0])
    axis.grid(True, axis="y", linestyle="--", alpha=0.5, linewidth=0.55)
    axis.tick_params(length=2.0, width=0.6, pad=1.4)
    # A conflict rate cannot exceed one, so the in-axes legend takes whatever
    # headroom the tallest bar leaves below the ceiling.
    axis.set_ylim(0, 1.0)

    axis.legend(
        handles=[
            Patch(color=STYLE["color_bind"], label="Bind race"),
            Patch(color=STYLE["color_stale"], label="Stale state"),
        ],
        loc="upper left", ncol=1, frameon=False,
        handlelength=0.9, handletextpad=0.35, labelspacing=0.2,
        borderaxespad=0.2,
    )
    axis.set_title("(a) Conflict decomposition\nunder parallelism", pad=2.5, loc="left")


def draw_right(axis, data: dict) -> None:
    """Effective parallelism vs scheduler count against the ideal diagonal."""

    x = data["sched_counts"]
    series = (
        ("base_event", STYLE["color_baseline"], STYLE["ls_event"],
         STYLE["marker_event"]),
        ("base_periodic", STYLE["color_baseline"], STYLE["ls_periodic"],
         STYLE["marker_periodic"]),
        ("parkour_event", STYLE["color_parkour"], STYLE["ls_event"],
         STYLE["marker_event"]),
        ("parkour_periodic", STYLE["color_parkour"], STYLE["ls_periodic"],
         STYLE["marker_periodic"]),
    )
    for key, color, linestyle, marker in series:
        axis.plot(x, data[key], color=color, linestyle=linestyle, marker=marker,
                  linewidth=STYLE["lw_main"], markersize=2.8)

    # Ideal drawn last so it stays visible where ParKour event nearly meets it.
    axis.plot(x, data["ideal"], color=STYLE["color_ideal"],
              linestyle=STYLE["ls_ideal"], linewidth=STYLE["lw_ideal"], zorder=10)

    axis.set_xticks(x)
    axis.set_xticklabels([str(int(value)) for value in x])
    axis.set_xlabel("Number of schedulers")
    axis.set_ylabel("Effective parallelism")
    axis.set_yticks([0, 5, 10, 15, 20])
    axis.set_ylim(0, 25.5)
    axis.grid(True, linestyle="--", alpha=0.5, linewidth=0.55)
    axis.tick_params(length=2.0, width=0.6, pad=1.4)

    # No in-axes legend: both encoding factors are named once at figure level so
    # that the five lines here map onto labels without the reader composing two
    # separate legends.
    axis.set_title("(b) ParKour recovers\nwasted parallelism", pad=2.5, loc="left")


def figure_legend_handles():
    """Handles naming both encoding factors of the figure.

    Line style, marker and bar hatch identify the synchronization paradigm and
    are shown in a neutral colour paired with the matching bar patch; colour
    then identifies the system and is shown with a neutral solid sample, so
    neither factor is mistaken for a third series. Together the five entries
    account for every line in panel (b) and every bar style in panel (a).

    The paradigm entries come first because they are what a reader must resolve
    to tell the panels' curves apart, the systems second, and the ideal
    reference last as the one entry that belongs to neither factor.

    The paradigm samples carry their markers because dash spacing alone is not
    separable at this font size: a reader who cannot tell the two line styles
    apart in a legend handle a few millimetres wide can still tell a circle from
    a square, and panel (b) draws those same markers on every point.
    """

    system_handles = [
        Line2D([], [], color=STYLE["color_baseline"], linestyle=STYLE["ls_event"],
               linewidth=STYLE["lw_main"]),
        Line2D([], [], color=STYLE["color_parkour"], linestyle=STYLE["ls_event"],
               linewidth=STYLE["lw_main"]),
        Line2D([], [], color=STYLE["color_ideal"], linestyle=STYLE["ls_ideal"],
               linewidth=STYLE["lw_ideal"]),
    ]
    system_labels = ["Vanilla", "ParKour", "Ideal"]

    paradigm_handles, paradigm_labels = [], []
    for hatch, linestyle, marker, label in (
        (STYLE["hatch_event"], STYLE["ls_event"], STYLE["marker_event"],
         "Event-driven"),
        (STYLE["hatch_periodic"], STYLE["ls_periodic"], STYLE["marker_periodic"],
         "Periodic"),
    ):
        paradigm_handles.append((
            Patch(facecolor="lightgray", hatch=hatch, edgecolor="black", linewidth=0.5),
            Line2D([], [], color=STYLE["color_single"], linestyle=linestyle,
                   linewidth=STYLE["lw_main"], marker=marker, markersize=2.8,
                   markevery=[0, -1]),
        ))
        paradigm_labels.append(label)

    return paradigm_handles + system_handles, paradigm_labels + system_labels


# ============================================================================
#  Top-level: load, render, save.
# ============================================================================

def build_figure(data: dict):
    """Build the two-panel Figure 3 paper artifact."""

    rates = _index(data)
    middle = load_middle(rates)
    right = load_right(rates)
    sns.set_style("ticks")
    with mpl.rc_context(RC_PARAMS):
        figure = plt.figure(figsize=FIG_SIZE)
        grid = GridSpec(1, 2, figure=figure, width_ratios=WIDTH_RATIOS, **LAYOUT)
        draw_middle(figure.add_subplot(grid[0, 0]), middle)
        draw_right(figure.add_subplot(grid[0, 1]), right)
        handles, labels = figure_legend_handles()
        figure.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 1.005),
                      ncol=len(labels), frameon=False,
                      handler_map={tuple: HandlerTuple(ndivide=None, pad=0.3)},
                      handlelength=2.6, handletextpad=0.3, columnspacing=0.6,
                      borderaxespad=0.0)
    return figure


def main() -> None:
    """Render Figure 3 only from its hash-locked verified cache."""

    data = load_verified(VERIFIED_CACHE, MANIFEST)
    if data["meta"]["fixed_constants"] != constants().to_dict():
        raise AssertionError("verified cache does not match the Figure 3 constants")
    # The font rcParams are read by the backend at save time, not at draw time,
    # so the save has to happen inside the same context that builds the figure.
    # Outside it the defaults return and the PDF comes back with Type 3 fonts,
    # which ACM camera-ready rejects.
    with mpl.rc_context(RC_PARAMS):
        figure = build_figure(data)
        for path in save_figure(figure, OUTPUT_DIR, "motivation", pad_inches=0.0):
            print(path)
    plt.close(figure)


if __name__ == "__main__":
    main()
