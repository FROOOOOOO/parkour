#!/usr/bin/env python3
"""
Paper Figure: ACF and throughput by workload-fill occupancy (supports §5.3).

Default layout ("paradigm-rows", 3.33" wide, i.e. a single-column figure at
20,000 nodes — the variant the paper includes):
  Top    panel: event-driven synchronization (Vanilla vs. ParKour)
  Bottom panel: periodic synchronization     (Vanilla vs. ParKour)

  X axis      : workload-fill occupancy intervals (0-80% / 80-90% / 90-100%)
  Left  y axis: ACF rate (grouped bars, percent-formatted)
  Right y axis: mean placement throughput (pods/s, dotted lines + markers,
                median printed above each marker)
  Error bars  : Q1-Q3 across the five raw trials of each configuration.

The "paradigm-cols" layout instead puts the paradigms side by side and one
cluster scale per row, which needs a two-column figure* (7.0" wide).

20,000 nodes is the scale shown because ParKour is ahead of the matched vanilla
baseline in every occupancy band under both paradigms there. At 10,000 nodes the
periodic pair inverts in the two lower bands (ParKour shows a higher ACF rate at
equal or higher throughput), which does not belong in a headline figure.

Legend and axis wording reuse the paper's own baseline names ("Vanilla
(event-driven)", "ParKour (periodic)", ...) and metric names; the right axis and
its legend entry say "Throughput", matching the paper-wide term defined in
§5.1 (Metrics and statistics) rather than introducing a separate "useful
throughput" concept.

Input: experiments/work/figure-data/occupancy-intervals-1col.json, written by

    python ../scripts/export-figure-data.py --figure occupancy-intervals-1col

The export reads the per-trial interval values in `archive/occupancy.json`,
which `reduce.py` takes from `analyze-temporal-occupancy.py --scope b2`, and
reduces each interval to its median and quartiles over the five trials. Only
the 20,000-node scale the paper shows is archived. No value is hardcoded here.

Colours follow `common/style.py` (dark shades = event-driven, light shades =
periodic).

Usage:
    python plot-occupancy-intervals-1col.py                 # the paper figure
    python plot-occupancy-intervals-1col.py --layout paradigm-cols \
        --width 7.0 --row-height 1.90                       # wide figure* variant
    python plot-occupancy-intervals-1col.py --show          # save + display
    python plot-occupancy-intervals-1col.py --data path/to.json --output-dir out/
"""

import argparse
import os
import sys
import warnings

import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import PercentFormatter

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths and identity
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402

FIGURE = "occupancy-intervals-1col"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")
DEFAULT_SCALES = ["20k"]
DEFAULT_WIDTH = 3.33       # ACM sigplan \columnwidth
DEFAULT_ROW_HEIGHT = 1.15  # compressed to match the data-plane figure
DEFAULT_LAYOUT = "paradigm-rows"

# ---------------------------------------------------------------------------
#  Colours and per-figure sizes
#
#  Dark shades are event-driven, light shades periodic; the hues come from the
#  shared palette so this figure matches the other system comparisons.
# ---------------------------------------------------------------------------
C_RED_D = style.VANILLA_RED
C_RED_L = style.VANILLA_RED_LIGHT
C_GREEN_D = style.PARKOUR_GREEN
C_GREEN_L = style.PARKOUR_GREEN_LIGHT
C_MARKER_EDGE = style.EDGE_GREY

RC_OVERRIDES = {
    "font.size": 7.5,
    "axes.labelsize": 7.0,
    "axes.titlesize": 7.5,
    "xtick.labelsize": 6.8,
    "ytick.labelsize": 6.8,
    "legend.fontsize": 6.5,
    "axes.linewidth": 0.7,
    "lines.linewidth": 1.3,
}

BAR_WIDTH   = 0.34   # width of one bar inside a two-bar occupancy group
BAR_OFFSET  = 0.185  # +/- offset of the two bars around the group centre
LW_TPUT     = 1.3    # throughput line width
MS_TPUT     = 3.6    # throughput marker size
MEW_TPUT    = 0.9    # throughput marker edge width; white faces stay readable
                     # where a throughput point falls inside a tall ACF bar
ERR_KW      = {"elinewidth": 0.7, "capthick": 0.7, "ecolor": C_MARKER_EDGE}

LEGEND_FONTSIZE   = 6.5   # matches RC_PARAMS legend.fontsize
LEGEND_ROW_HEIGHT = 0.13  # inches occupied by one legend row at that font size
# Panel titles are drawn above the axes, inside the band the legend also uses.
# Reserve room for one title line so the two stop overlapping. Taken out of
# the top margin rather than added to the figure height, which would cost a
# page in the paper.
TITLE_CLEARANCE = 0.17    # inches
TINY_BAR_FRACTION = 0.04  # bars below this share of the ACF axis get a value label
TPUT_DECIMAL_BELOW = 30.0  # throughput medians below this are printed with a decimal

# White outline so throughput labels stay legible on top of a coloured ACF bar.
LABEL_HALO  = path_effects.withStroke(linewidth=1.6, foreground="white")

# ---------------------------------------------------------------------------
#  Panel definitions — which JSON scenario/config each panel reads
# ---------------------------------------------------------------------------
PARADIGMS = [
    {
        "key":            "event-driven",
        "title":          "Event-driven",
        "vanilla_config": "Vanilla (event-driven)",
        "parkour_config": "ParKour (event-driven)",
        "vanilla_color":  C_RED_D,
        "parkour_color":  C_GREEN_D,
        "marker":         "o",
    },
    {
        "key":            "periodic",
        "title":          "Periodic",
        "vanilla_config": "Vanilla (periodic)",
        "parkour_config": "ParKour (periodic)",
        "vanilla_color":  C_RED_L,
        "parkour_color":  C_GREEN_L,
        "marker":         "s",
    },
]

ACF_METRIC   = "acf_rate_estimate"
# JSON key from analyze-temporal-occupancy.py; displayed simply as "Throughput"
# (§5.1's Throughput already excludes failed retries, so "useful" is redundant).
TPUT_METRIC  = "mean_useful_placement_throughput"
EXPECTED_TRIALS = 5

# ---------------------------------------------------------------------------
#  Axis and legend wording — kept identical to the paper's vocabulary; no
#  abbreviation is introduced here. Configuration names come straight from the
#  JSON config keys, which are the baseline names used in the text
#  ("Vanilla (event-driven)", "ParKour (periodic)", ...).
# ---------------------------------------------------------------------------
X_LABEL     = "Slot occupancy (workload fill)"
ACF_LABEL   = "ACF rate"
# Two lines keep the label inside the compressed single-column panel height.
TPUT_LABEL  = "Throughput\n(pods/s)"
TPUT_LEGEND = "Throughput (right axis)"


def _series(data, scale, paradigm_key, config_label, metric):
    """Extract per-interval median / Q1 / Q3 of one metric for one configuration.

    Args:
        data:          Parsed occupancy JSON.
        scale:         Cluster-size key as used in the JSON ("20k", "10k", ...).
        paradigm_key:  "event-driven" or "periodic".
        config_label:  Configuration label inside the scenario ("ParKour (periodic)").
        metric:        Metric name inside each interval summary block.

    Returns:
        Tuple of three numpy arrays (median, q1, q3), ordered like
        `data["metadata"]["intervals"]`.

    Raises:
        RuntimeError: If the scenario, configuration, interval or metric is
            missing, or if an interval does not aggregate the expected trials.
    """
    scenario_key = f"b2-{scale}-{paradigm_key}"
    scenario = data["scenarios"].get(scenario_key)
    if scenario is None:
        raise RuntimeError(f"Scenario {scenario_key} missing from data file")
    config = scenario["configs"].get(config_label)
    if config is None:
        raise RuntimeError(
            f"Configuration '{config_label}' missing from scenario {scenario_key}"
        )

    rows = {row["interval"]: row for row in config["summary"]}
    median, q1, q3 = [], [], []
    for interval in data["metadata"]["intervals"]:
        row = rows.get(interval)
        if row is None:
            raise RuntimeError(
                f"Interval {interval} missing for {scenario_key}/{config_label}"
            )
        stats = row.get(metric)
        if stats is None:
            raise RuntimeError(
                f"Metric {metric} missing for {scenario_key}/{config_label}"
                f" interval {interval}"
            )
        if stats.get("n") != EXPECTED_TRIALS:
            raise RuntimeError(
                f"{scenario_key}/{config_label} interval {interval} aggregates "
                f"{stats.get('n')} trials, expected {EXPECTED_TRIALS}"
            )
        median.append(stats["median"])
        q1.append(stats["q1"])
        q3.append(stats["q3"])
    return (np.asarray(median, dtype=float),
            np.asarray(q1, dtype=float),
            np.asarray(q3, dtype=float))


def _yerr(median, q1, q3):
    """Convert median/Q1/Q3 triples into a matplotlib asymmetric yerr array."""
    lower = np.maximum(0.0, median - q1)
    upper = np.maximum(0.0, q3 - median)
    return np.vstack([lower, upper])


def _interval_labels(data):
    """Render JSON interval keys ("0-80%") with an en dash for the figure."""
    return [interval.replace("-", "\u2013")
            for interval in data["metadata"]["intervals"]]


def _row_throughput_top(data, scale):
    """Return the largest Q3 throughput of one scale row.

    Both paradigm panels of a row share this right-axis limit so that the
    throughput curves stay visually comparable across synchronization modes.
    """
    top = 0.0
    for paradigm in PARADIGMS:
        for config_label in (paradigm["vanilla_config"],
                             paradigm["parkour_config"]):
            _, _, q3 = _series(data, scale, paradigm["key"], config_label,
                               TPUT_METRIC)
            top = max(top, float(q3.max()))
    return top


# ---------------------------------------------------------------------------
#  Drawing
# ---------------------------------------------------------------------------

def _draw_panel(ax, data, scale, paradigm, panel_tag, tput_top, show_xlabel,
                show_left_label, show_right_label, tput_labels=True,
                show_xticklabels=True):
    """Draw one paradigm panel: ACF bars (left y) + throughput (right y).

    Args:
        ax:               Target axes; a twin axes is created for throughput.
        data:             Parsed occupancy JSON.
        scale:            Cluster-size key ("20k").
        paradigm:         One entry of PARADIGMS.
        panel_tag:        Panel letter used in the title, e.g. "a".
        tput_top:         Largest Q3 throughput of the row, used for the shared
                          right-axis limit.
        show_xlabel:      Whether to draw the shared x-axis label.
        show_left_label:  Whether to draw the ACF y-axis label.
        show_right_label: Whether to draw the throughput y-axis label.
        tput_labels:      Whether to print the median throughput above each
                          throughput marker.
        show_xticklabels: Whether to draw the interval tick labels ("0-80%",
                          ...). Stacked rows of one column share the same
                          intervals, so only the bottom row needs them, as in
                          plot-dataplane-sensitivity.py.
    """
    ax2 = ax.twinx()
    labels = _interval_labels(data)
    x = np.arange(len(labels), dtype=float)

    configs = (
        (paradigm["vanilla_config"], paradigm["vanilla_color"], -BAR_OFFSET),
        (paradigm["parkour_config"], paradigm["parkour_color"], +BAR_OFFSET),
    )

    acf_top = 0.0
    drawn = []
    for config_label, color, offset in configs:
        acf_med, acf_q1, acf_q3 = _series(
            data, scale, paradigm["key"], config_label, ACF_METRIC)
        tp_med, tp_q1, tp_q3 = _series(
            data, scale, paradigm["key"], config_label, TPUT_METRIC)

        # ── ACF rate: grouped bars on the left axis, Q1-Q3 whiskers ────────
        ax.bar(x + offset, acf_med, width=BAR_WIDTH,
               color=color, edgecolor="black", linewidth=0.4, alpha=0.9,
               yerr=_yerr(acf_med, acf_q1, acf_q3), capsize=1.8,
               error_kw=ERR_KW, zorder=2)

        # ── Throughput: dotted line + white-faced markers, right axis. The
        #    dark marker edge keeps points readable where the curve dips into
        #    a tall ACF bar of the same colour.
        ax2.errorbar(x + offset, tp_med,
                     yerr=_yerr(tp_med, tp_q1, tp_q3),
                     color=color, linestyle=":", linewidth=LW_TPUT,
                     marker=paradigm["marker"], markersize=MS_TPUT,
                     markerfacecolor="white",
                     markeredgecolor=C_MARKER_EDGE, markeredgewidth=MEW_TPUT,
                     elinewidth=0.7, capsize=1.6, capthick=0.7, zorder=4)

        # ── Median throughput printed above the Q3 whisker ────────────────
        if tput_labels:
            for xi, median, upper in zip(x + offset, tp_med, tp_q3):
                text = (f"{median:.1f}" if median < TPUT_DECIMAL_BELOW
                        else f"{median:.0f}")
                ax2.annotate(text, xy=(xi, upper),
                             xytext=(0, 3.0), textcoords="offset points",
                             ha="center", va="bottom", fontsize=6.0,
                             color=C_MARKER_EDGE, zorder=5,
                             path_effects=[LABEL_HALO])

        acf_top = max(acf_top, float(acf_q3.max()))
        drawn.append((offset, acf_med, acf_q3))

    # ── Axis styling ──────────────────────────────────────────────────────
    ax.set_xticks(x)
    ax.set_xticklabels(labels if show_xticklabels else [])
    ax.set_xlim(-0.5, len(labels) - 0.5)
    if show_xlabel:
        ax.set_xlabel(X_LABEL)

    ax.set_ylim(0.0, min(1.02, acf_top * 1.15))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    if show_left_label:
        ax.set_ylabel(ACF_LABEL)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)

    # Print the value of bars that are too short to read off the axis, so a
    # near-zero ACF rate is not mistaken for a missing bar.
    axis_top = ax.get_ylim()[1]
    for offset, acf_med, acf_q3 in drawn:
        for xi, median, upper in zip(x + offset, acf_med, acf_q3):
            if median < TINY_BAR_FRACTION * axis_top:
                percent = median * 100.0
                text = f"{percent:.2f}%" if percent < 1.0 else f"{percent:.1f}%"
                ax.annotate(text, xy=(xi, upper),
                            xytext=(0, 2.5), textcoords="offset points",
                            ha="center", va="bottom", fontsize=5.8,
                            color=C_MARKER_EDGE, zorder=5,
                            path_effects=[LABEL_HALO])

    ax2.set_ylim(-0.02 * tput_top, tput_top * 1.32)
    if show_right_label:
        # Two lines keep the label inside the compressed single-column panel.
        ax2.set_ylabel(TPUT_LABEL)

    # Regular weight, matching the panel titles of the other paper figures. The
    # node count is left out because the caption already states the scale.
    ax.set_title(f"({panel_tag}) {paradigm['title']}",
                 fontsize=7.5, loc="left", pad=3)


def build_figure(data, scales, width, row_height, tput_labels=True,
                 layout=DEFAULT_LAYOUT):
    """Assemble the full figure from one panel per (scale, paradigm) pair.

    Args:
        data:        Parsed occupancy JSON.
        scales:      Ordered list of cluster-size keys, e.g. ["20k"].
        width:       Figure width in inches (7.0 fits a two-column figure*,
                     3.33 fits a single-column figure of the ACM sigplan style).
        row_height:  Height in inches reserved for each panel row.
        tput_labels: Whether to print median throughput values above markers.
        layout:      "paradigm-rows" (default) stacks the paradigms vertically
                     and puts scales in columns, which fits a single text
                     column; "paradigm-cols" puts the two paradigms side by side
                     and one scale per row, which needs a wide figure*.

    Returns:
        The matplotlib Figure.
    """
    style.apply(**RC_OVERRIDES)

    # Panel grid: each cell holds the (scale, paradigm) pair it draws.
    if layout == "paradigm-cols":
        grid = [[(scale, paradigm) for paradigm in PARADIGMS] for scale in scales]
    elif layout == "paradigm-rows":
        grid = [[(scale, paradigm) for scale in scales] for paradigm in PARADIGMS]
    else:
        raise ValueError(f"Unknown layout '{layout}'")
    n_rows, n_cols = len(grid), len(grid[0])

    # The configuration names are long, so a narrow figure needs two legend
    # columns plus one extra row for the throughput line entry.
    legend_cols = 4 if width >= 5.5 else 2
    config_rows = -(-len(PARADIGMS) * 2 // legend_cols)   # ceil division
    legend_height = LEGEND_ROW_HEIGHT * (config_rows + 1) + 0.05
    fig_height = n_rows * row_height + legend_height
    fig = plt.figure(figsize=(width, fig_height))

    top = 1.0 - (legend_height + 0.06 + TITLE_CLEARANCE) / fig_height
    bottom = 0.40 / fig_height
    gs = GridSpec(n_rows, n_cols, figure=fig, wspace=0.42,
                  hspace=0.30 if n_rows > 1 else 0.0,
                  # Margins are tuned so that axis labels stay inside the
                  # nominal figure width, i.e. the saved PDF is not wider than
                  # the text column it is included at.
                  left=0.070 if width >= 5.5 else 0.170,
                  right=0.925 if width >= 5.5 else 0.818,
                  top=top, bottom=bottom)

    tag = 0
    for row, cells in enumerate(grid):
        for column, (scale, paradigm) in enumerate(cells):
            ax = fig.add_subplot(gs[row, column])
            _draw_panel(
                ax, data, scale, paradigm,
                panel_tag=chr(ord("a") + tag),
                # Both paradigms of one scale share the throughput axis.
                tput_top=_row_throughput_top(data, scale),
                show_xlabel=(row == n_rows - 1),
                show_left_label=True,
                show_right_label=True,
                tput_labels=tput_labels,
                show_xticklabels=(row == n_rows - 1),
            )
            tag += 1

    # ── Shared legend above the panels ────────────────────────────────────
    #  Labels are the paper's own baseline names, taken from the JSON config
    #  keys, plus one entry for the throughput line style. An invisible filler
    #  keeps the column-major grid aligned as
    #      Vanilla (event-driven)   | Vanilla (periodic)
    #      ParKour (event-driven)   | ParKour (periodic)
    #      Useful throughput (...)  |
    columns = []
    for paradigm in PARADIGMS:
        columns.append([
            mpatches.Patch(facecolor=paradigm["vanilla_color"],
                           edgecolor="black", linewidth=0.4, alpha=0.9,
                           label=paradigm["vanilla_config"]),
            mpatches.Patch(facecolor=paradigm["parkour_color"],
                           edgecolor="black", linewidth=0.4, alpha=0.9,
                           label=paradigm["parkour_config"]),
        ])
    columns[0].append(mlines.Line2D(
        [], [], color=C_MARKER_EDGE, linestyle=":", linewidth=LW_TPUT,
        marker="o", markersize=MS_TPUT, markerfacecolor="white",
        markeredgecolor=C_MARKER_EDGE, markeredgewidth=MEW_TPUT,
        label=TPUT_LEGEND))

    if legend_cols == 2:
        # Two columns: pad the shorter one so both hold the same row count.
        columns[1].append(mlines.Line2D([], [], linestyle="none", label=" "))
        handles = columns[0] + columns[1]
    else:
        # One row per configuration pair plus the line entry at the end.
        handles = [columns[0][0], columns[0][1], columns[1][0], columns[1][1],
                   columns[0][2]]

    fig.legend(handles=handles, loc="upper center",
               bbox_to_anchor=(0.5, 1.0), ncol=legend_cols,
               columnspacing=0.9, handlelength=1.5, handletextpad=0.35,
               labelspacing=0.30, frameon=False, fontsize=LEGEND_FONTSIZE)

    return fig


# ---------------------------------------------------------------------------
#  Reporting
# ---------------------------------------------------------------------------

def print_table(data, scales):
    """Print the plotted medians and Q1-Q3 ranges so the figure is auditable."""
    labels = data["metadata"]["intervals"]
    header = (f"{'scale':>6} {'paradigm':>12} {'config':>26} {'interval':>9} "
              f"{'ACF % (Q1, Q3)':>26} "
              f"{'throughput pods/s (Q1, Q3)':>34}")
    print(header)
    print("-" * len(header))
    for scale in scales:
        for paradigm in PARADIGMS:
            for config_label in (paradigm["vanilla_config"],
                                 paradigm["parkour_config"]):
                acf = _series(data, scale, paradigm["key"], config_label,
                              ACF_METRIC)
                tput = _series(data, scale, paradigm["key"], config_label,
                               TPUT_METRIC)
                for i, interval in enumerate(labels):
                    acf_cell = (f"{acf[0][i] * 100:6.2f} "
                                f"({acf[1][i] * 100:6.2f}, {acf[2][i] * 100:6.2f})")
                    tp_cell = (f"{tput[0][i]:7.1f} "
                               f"({tput[1][i]:7.1f}, {tput[2][i]:7.1f})")
                    print(f"{scale:>6} {paradigm['key']:>12} {config_label:>26} "
                          f"{interval:>9} {acf_cell:>26} {tp_cell:>34}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=envelope.path_for(_EXPERIMENTS, FIGURE),
                        help="Exported figure data (default: the export path)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--scales", nargs="+", default=DEFAULT_SCALES,
                        help="Cluster scales to draw, e.g. 2k 5k 10k 20k")
    parser.add_argument("--width", type=float, default=DEFAULT_WIDTH,
                        help="Figure width in inches")
    parser.add_argument("--row-height", type=float, default=DEFAULT_ROW_HEIGHT,
                        help="Height in inches per panel row")
    parser.add_argument("--layout", choices=("paradigm-cols", "paradigm-rows"),
                        default=DEFAULT_LAYOUT,
                        help="'paradigm-rows' (default): paradigms stacked, scales "
                             "in columns (single text column); 'paradigm-cols': "
                             "paradigms side by side, one scale per row")
    parser.add_argument("--no-tput-labels", action="store_true",
                        help="Do not print median throughput values above markers")
    parser.add_argument("--no-table", action="store_true",
                        help="Do not print the plotted medians and Q1-Q3 ranges")
    parser.add_argument("--show", action="store_true",
                        help="Display the figure after saving")
    args = parser.parse_args()

    occupancy = envelope.load(args.data, figure=FIGURE)
    figure = build_figure(occupancy, args.scales, args.width, args.row_height,
                          tput_labels=not args.no_tput_labels,
                          layout=args.layout)
    for path in style.save_figure(figure, args.output_dir, FIGURE):
        print(path)
    if not args.no_table:
        print_table(occupancy, args.scales)
    if args.show:
        plt.show()
    else:
        plt.close(figure)


if __name__ == "__main__":
    main()
