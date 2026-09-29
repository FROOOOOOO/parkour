#!/usr/bin/env python3
"""
Paper Figure: data-plane delay/failure sensitivity (module F, supports §5.7).

Layout (single column, 3.33" wide, two stacked panels sharing the x axis):
  (a) All-candidates-failed rate (ACF), log y axis.
  (b) Bind throughput in pods/s, linear y axis.
  Both panels: x = three data-plane profiles (none / delay / delay+fail),
  four bars per group = event-driven K=0, event-driven K=2, globSync K=0,
  globSync K=2.  Whiskers span the min and max of that cell's trials
  (n=3 event-driven, n=5 periodic).

Colour encodes the mechanism setting (red = K=0 same-code baseline, green =
K=2 with w=0.5), shade encodes the synchronization paradigm (dark =
event-driven, light = single-partition globSync at G=1 s), matching
plot-occupancy-intervals-1col.py.

Data
----
Input: experiments/work/figure-data/dataplane-sensitivity.json, written by

    python ../scripts/export-figure-data.py --figure dataplane-sensitivity

from `archive/dataplane.json`, which `reduce.py` flattens from the per-round
summaries of the anchored module-F campaign, collected 2026-09-12..13. It is the
only campaign whose data may enter the paper. The two earlier batches (the
45-round pilot at K=4 / diffSync M=5 with Gödel arms, and the 36-round
synthetic-profile batch) are void: the pilot's parameters do not match the
design, and both were hit by the KWOK dual-controller defect that made `Dreal`
rounds actually measure `Z0` behaviour. They are not archived.

  49 rounds, 48 kept, one record per round:
    F1-E  E2 vs E3, Z0 + Dreal,   3 trials, 12 rounds
    F1-P  P1 vs P4, Z0 + Dreal,   5 trials, 20 rounds
    F2-E  E2 vs E3, Dreal-F1,     3 trials,  6 rounds
    F2-P  P1 vs P4, Dreal-F1,     5 trials, 10 rounds

The single round not kept is F2-P order 9, whose observed startup-failure rate
put the configured 1% outside its Wilson interval; it is dropped here and kept
in the archive for audit.

Delay profile: `Dreal` is four weighted buckets
`[581, 830] / (830, 1109] / (1109, 2384] / (2384, 4768]` ms, weights
5000/4000/900/100, derived from the upstream Kubernetes scalability CI job
`gce-5000Nodes` in its burst phase (pod-startup P50/P90/P99 =
830 / 1109 / 2384 ms), with the two ends bounded by 0.7*P50 and 2*P99.
Theoretical mean 0.933 s, measured 0.96 s.  `Dreal-F1` adds a 1% post-bind
startup-failure injection.  The buckets are anchored to public real-node
measurements; the injection itself is synthetic (a KWOK Stage).

Metric definitions
------------------
Panel (b) plots `q_bind_at_t99` = 9900 / t99, the placement rate at a uniform
completion cut point, which the campaign prescribes for cross-condition
comparison.  The archived `throughput_raw_pods_per_s`
divides 10,000 pods by *each round's own* window end, so it measures how long a
round ran rather than how fast it scheduled, and under the periodic profiles the
endgame dominates that window: at one pod per node with zero headroom the run
only finishes once the last pending pod is matched to the last free node, which
a periodic view reveals once per G.  Tails reach 74-76% of the whole window
there, against 3.6-8.0% on the event-driven arms, so the raw field inflates the
periodic delay gain to +414% and deflates the delay+fail one to +92%; at T99
both read +53%.  Exporting with `--throughput-metric raw` reproduces the
whole-window view.

Right-censoring only affects the raw field.  Three F2-P rounds hit the runner's
1000 s ceiling and are archived at the 10.0 pods/s floor, but the truncation
happens after 9999/10000 pods are placed, so t90 and t99 are fully observed and
the rounds are valid steady-state data.  They are hatched in panel (b) only
when the raw metric is selected.

Panel (a) plots ACF over the same [round start, T99] prefix, so both panels
share one cut point.  The archived `acf_rate` is a whole-window aggregate and
the endgame is pure conflict, which inflates it on exactly the two periodic
data-plane profiles whose tails are long: vanilla at `delay` reads 74.4% over
the whole window against 52.9% up to T99, and ParKour at `delay+fail` reads
58.6% against 28.4%.  The event-driven arms and periodic `none` move by at most
0.06 pp between the two, which is how we know the cut point itself introduces
no offset.  Exporting with `--acf-metric whole` reproduces the whole-window view.

The windowed values (`t99_acf_rate` in the archive) were rebuilt from the
binder's raw Prometheus counters at 1 s resolution rather than from the 15 s
range series: the binder restarts once per round so its counters start at zero,
making the prefix rate acf(T)/(bind_success(T)+acf(T)) with the runner's own
denominator convention.  Every round was checked against its round summary
before use.

Usage:
    python plot-dataplane-sensitivity.py
    python plot-dataplane-sensitivity.py --show
    python plot-dataplane-sensitivity.py --data path/to.json --output-dir out/
"""

import argparse
import os
import sys
import warnings

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import (FuncFormatter, LogLocator, MultipleLocator,
                               NullFormatter)

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402

FIGURE = "dataplane-sensitivity"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")
DEFAULT_WIDTH = 3.33       # ACM sigplan \columnwidth
DEFAULT_HEIGHT = 1.28      # panel area, excluding the legend strip

ACF_PANEL_LABEL = "(a) ACF%"
TPUT_PANEL_LABEL = "(b) Pods/s"

PROFILES = ["none", "delay", "delay+fail"]
PARADIGMS = ["event", "periodic"]
BARS = [("event", 0), ("event", 2), ("periodic", 0), ("periodic", 2)]
# ---------------------------------------------------------------------------
#  Colours and per-figure sizes
#
#  Dark shades are event-driven, light shades periodic, matching the occupancy
#  figure so the two compressed single-column panels read as a pair.
# ---------------------------------------------------------------------------
C_RED_D = style.VANILLA_RED
C_RED_L = style.VANILLA_RED_LIGHT
C_GREEN_D = style.PARKOUR_GREEN
C_GREEN_L = style.PARKOUR_GREEN_LIGHT
C_EDGE = style.EDGE_GREY

BAR_COLOR = {
    ("event", 0): C_RED_D,
    ("event", 2): C_GREEN_D,
    ("periodic", 0): C_RED_L,
    ("periodic", 2): C_GREEN_L,
}

RC_OVERRIDES = {
    "font.size": 7.5,
    "axes.labelsize": 7.0,
    "axes.titlesize": 7.5,
    "xtick.labelsize": 6.8,
    "ytick.labelsize": 6.8,
    "legend.fontsize": 6.5,
    "axes.linewidth": 0.7,
}
ANNOT_FONTSIZE = 6.5       # printed size floor for this paper
ERR_KW = {"elinewidth": 0.6, "capthick": 0.6, "ecolor": C_EDGE, "capsize": 1.2}


# ---------------------------------------------------------------------------
#  Loading — one record per valid round
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Drawing
# ---------------------------------------------------------------------------

def _acf_label(percent):
    """Value printed above an ACF bar: 2 decimals below 1, 1 decimal below 10."""
    if percent < 1:
        return f"{percent:.2f}"
    if percent < 10:
        return f"{percent:.1f}"
    return f"{percent:.0f}"


def _percent_fmt(value, _pos=None):
    """Log-axis tick label, bare: 0.5 -> '0.5', 5 -> '5', 50 -> '50'.

    The unit rides in the panel's y label instead of on every tick, which keeps
    the two panels' tick columns the same width so their labels line up.
    """
    return f"{value:g}" if value < 1 else f"{value:.0f}"


def _draw_panel(ax, cells, metric, log_scale, floor, top, ylabel, hatch_censored=False):
    """Draw one panel: four grouped bars per profile with min-max whiskers.

    Args:
        ax:        Target axes.
        cells:     Aggregated cells from aggregate().
        metric:    "acf" (drawn in percent) or "tput" (pods/s).
        log_scale: Whether the y axis is logarithmic.
        floor:     Bottom of the y axis (also the bar baseline on a log axis).
        top:       Top of the y axis.
        ylabel:    Y-axis label, which also carries the panel tag.
        hatch_censored: Mark cells holding a right-censored round. Only
            meaningful for the whole-window throughput, whose mean is then a
            lower bound; the T99 cut point is observed in every round.
    """
    x = np.arange(len(PROFILES), dtype=float)
    offsets = [-0.30, -0.10, 0.10, 0.30]
    scale = 100.0 if metric == "acf" else 1.0
    for offset, (paradigm, k) in zip(offsets, BARS):
        for xi, profile in zip(x + offset, PROFILES):
            cell = cells[(paradigm, k, profile)]
            value = cell[f"{metric}_mean"] * scale
            low = cell[f"{metric}_min"] * scale
            high = cell[f"{metric}_max"] * scale
            hatch = ("///" if (hatch_censored and metric == "tput"
                               and cell["censored"]) else None)
            ax.bar(xi, value - floor, bottom=floor, width=0.185,
                   color=BAR_COLOR[(paradigm, k)], edgecolor=C_EDGE,
                   linewidth=0.4, zorder=3, hatch=hatch,
                   yerr=[[max(0.0, value - low)], [max(0.0, high - value)]],
                   error_kw=ERR_KW)
            text = (_acf_label(value) if metric == "acf" else f"{value:.0f}")
            ax.annotate(text, xy=(xi, high), xytext=(0, 1.2),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=ANNOT_FONTSIZE, color=C_EDGE, zorder=5)
    if log_scale:
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(LogLocator(base=10, numticks=5))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.yaxis.set_major_formatter(FuncFormatter(_percent_fmt))
    else:
        ax.yaxis.set_major_locator(MultipleLocator(100))
    ax.set_ylim(floor, top)
    ax.set_xticks(x)
    ax.set_xticklabels(PROFILES)
    ax.set_xlim(-0.5, len(PROFILES) - 0.5)
    ax.set_ylabel(ylabel, labelpad=1.5)
    ax.grid(True, axis="y", which="major", ls="--", lw=0.5, alpha=0.45)
    ax.set_axisbelow(True)
    # Top/right stay hidden here: the bar-top value labels sit flush against
    # the axes ceiling and collide with a top spine.
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def build_figure(cells, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                 throughput_metric="t99", acf_metric="t99"):
    """Assemble the two-panel single-column figure."""
    style.apply(**RC_OVERRIDES)

    legend_height = 0.28
    total = height + legend_height
    fig = plt.figure(figsize=(width, total))
    gs = GridSpec(2, 1, figure=fig, height_ratios=[1.0, 1.0], hspace=0.30,
                  left=0.168, right=0.995,
                  top=1.0 - (legend_height + 0.06) / total,
                  bottom=0.40 / total)
    ax_acf = fig.add_subplot(gs[0, 0])
    # Headroom above the tallest bar for its printed value, on a log axis.
    acf_top = 110 if acf_metric != "whole" else 165
    _draw_panel(ax_acf, cells, "acf", True, 0.35, acf_top,
                ACF_PANEL_LABEL)
    ax_acf.set_xticklabels([])
    ax_tput = fig.add_subplot(gs[1, 0])
    _draw_panel(ax_tput, cells, "tput", False, 0.0, 355,
                TPUT_PANEL_LABEL,
                hatch_censored=(throughput_metric == "raw"))
    shared_cut = (throughput_metric == "t99" and acf_metric == "t99")
    ax_tput.set_xlabel(
        "Data-plane profile" + (" (both panels over $[0,T_{99}]$)"
                                if shared_cut else ""), labelpad=1.5)

    # Legend names follow the paper's baseline vocabulary (section 5.1). The
    # parameter settings that distinguish these runs from the deployed default
    # (K, w, partition count) belong in the figure caption, not the legend.
    handles = [
        mpatches.Patch(facecolor=C_RED_D, edgecolor=C_EDGE, linewidth=0.4,
                       label="Vanilla (event-driven)"),
        mpatches.Patch(facecolor=C_GREEN_D, edgecolor=C_EDGE, linewidth=0.4,
                       label="ParKour (event-driven)"),
        mpatches.Patch(facecolor=C_RED_L, edgecolor=C_EDGE, linewidth=0.4,
                       label="Vanilla (periodic)"),
        mpatches.Patch(facecolor=C_GREEN_L, edgecolor=C_EDGE, linewidth=0.4,
                       label="ParKour (periodic)"),
    ]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0),
               ncol=2, columnspacing=0.9, handlelength=1.4, handletextpad=0.35,
               labelspacing=0.18, frameon=False,
               fontsize=RC_OVERRIDES["legend.fontsize"])
    return fig


# ---------------------------------------------------------------------------
#  Reporting
# ---------------------------------------------------------------------------

def print_cells(cells):
    """Print the plotted cell statistics and the K=2 vs K=0 comparison."""
    header = (f"{'paradigm':<10}{'profile':<12}{'ACF K=0':>9}{'ACF K=2':>9}"
              f"{'ACF red.':>9}{'(whole)':>9}{'tput K=0':>10}{'tput K=2':>10}"
              f"{'gain':>8}{'tail K=0':>9}{'tail K=2':>9}")
    print(header)
    print("-" * len(header))
    for paradigm in PARADIGMS:
        for profile in PROFILES:
            k0 = cells[(paradigm, 0, profile)]
            k2 = cells[(paradigm, 2, profile)]
            whole = (1 - k2["acf_whole_mean"] / k0["acf_whole_mean"]) * 100
            print(f"{paradigm:<10}{profile:<12}"
                  f"{k0['acf_mean'] * 100:>8.2f}%{k2['acf_mean'] * 100:>8.2f}%"
                  f"{(1 - k2['acf_mean'] / k0['acf_mean']) * 100:>8.1f}%"
                  f"{whole:>8.1f}%"
                  f"{k0['tput_mean']:>10.2f}{k2['tput_mean']:>10.2f}"
                  f"{(k2['tput_mean'] / k0['tput_mean'] - 1) * 100:>+7.1f}%"
                  f"{k0['tail_fraction'] * 100:>8.1f}%{k2['tail_fraction'] * 100:>8.1f}%")
    censored_cells = {k: v for k, v in cells.items() if v["censored"]}
    if censored_cells:
        print("\nright-censored cells (throughput is a lower bound):")
        for key, cell in sorted(censored_cells.items()):
            print(f"  {key[0]:<9} K={key[1]} {key[2]:<11} "
                  f"{cell['censored']}/{cell['n']} rounds at the 1000 s ceiling; "
                  f"mean {cell['tput_mean']:.2f} pods/s, "
                  f"uncensored-only {cell['tput_mean_uncensored']:.2f}")


def print_audit(audit):
    """Print the failure-gate trail that the validity paragraph quotes."""
    kept = [a for a in audit if a["kept"]]
    dropped = [a for a in audit if not a["kept"]]
    rates = [a["failure_rate"] * 100 for a in kept]
    print(f"failure matrices: {len(kept)} kept rounds, "
          f"observed startup-failure rate {min(rates):.2f}%-{max(rates):.2f}%, "
          f"gate pass {sum(1 for a in kept if a['gate_pass'])}/{len(kept)}, "
          f"failure semantics valid "
          f"{sum(1 for a in kept if a['semantics_ok'])}/{len(kept)}")
    for entry in dropped:
        print(f"  dropped: {entry['matrix']} order {entry['order']} "
              f"{entry['method']}, failure rate "
              f"{entry['failure_rate'] * 100:.4f}%")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=envelope.path_for(_EXPERIMENTS, FIGURE),
                        help="Exported figure data (default: the export path)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--width", type=float, default=DEFAULT_WIDTH,
                        help="Figure width in inches (3.33 = sigplan column width)")
    parser.add_argument("--height", type=float, default=DEFAULT_HEIGHT,
                        help="Panel area height in inches, excluding the legend")
    parser.add_argument("--no-table", action="store_true",
                        help="Do not print the plotted cells and the gate audit")
    parser.add_argument("--show", action="store_true",
                        help="Display the figure after saving")
    args = parser.parse_args()

    data = envelope.load(args.data, figure=FIGURE)
    # The export lists the cells; the drawing code looks them up by coordinate.
    cells = {(cell["paradigm"], cell["k"], cell["profile"]): cell
             for cell in data["cells"]}

    if not args.no_table:
        print(f"throughput metric: {data['throughput_metric']}")
        print(f"ACF metric:        {data['acf_metric']}")
        print()
        print_cells(cells)
        print()
        print_audit(data["audit"])
        print()

    figure = build_figure(cells, args.width, args.height,
                          data["throughput_metric"], data["acf_metric"])
    for path in style.save_figure(figure, args.output_dir, FIGURE,
                                  dpi=300, pad_inches=0.01):
        print(path)

    if args.show:
        plt.show()
    else:
        plt.close(figure)


if __name__ == "__main__":
    main()
