#!/usr/bin/env python3
"""Figure `trace-arrival-rate`: how bursty a production arrival stream is.

Two panels drawn from the Alibaba cluster trace:

  (a) the distribution of arrival rates over the active period, as CDFs at two
      window sizes, against the single-scheduler reference rate;
  (b) one representative 60 s window at 1 s resolution, so the reader sees an
      actual burst rather than only its summary.

Panel (b) keeps a linear y axis on purpose: it shows at a glance how far the
per-second rate sits above the reference, which a log axis would compress, and
it represents the seconds with no arrivals exactly.

Input
    experiments/work/figure-data/trace-arrival-rate.json, written by

        python ../trace/alibaba2018/export.py --data-dir <trace-dir>

    The trace is not redistributed; that script's docstring gives the download.
    This script holds no measurements of its own.

Usage
    python plot-trace-arrival-rate.py
    python plot-trace-arrival-rate.py --show
"""

from __future__ import annotations

import argparse
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

import matplotlib.lines as mlines  # noqa: E402
import matplotlib.patheffects as path_effects  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import (  # noqa: E402
    FuncFormatter,
    LogLocator,
    MultipleLocator,
    NullFormatter,
)

from common import data as envelope  # noqa: E402
from common import style  # noqa: E402

FIGURE = "trace-arrival-rate"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")

DEFAULT_WIDTH = 3.33       # ACM sigplan \columnwidth
DEFAULT_HEIGHT = 1.62
DEFAULT_CDF_X_FLOOR = 1.0

# ---------------------------------------------------------------------------
#  Colours and per-figure sizes
# ---------------------------------------------------------------------------
C_RED_D = style.VANILLA_RED       # cap / reference rules
C_BLUE_D = style.REFERENCE_BLUE   # 1 s windows
C_GREEN_D = style.PARKOUR_GREEN   # 60 s windows
C_GREY = style.NEUTRAL_GREY       # neutral annotations

WINDOW_STYLE = {
    1:  {"color": C_BLUE_D,  "ls": "-",  "label": "1 s windows",
         "short": "1 s"},
    10: {"color": C_GREY,    "ls": "-.", "label": "10 s windows",
         "short": "10 s"},
    60: {"color": C_GREEN_D, "ls": "--", "label": "60 s windows",
         "short": "60 s"},
}

RC_OVERRIDES = {
    "font.size": 8,
    "axes.labelsize": 7.5,
    "axes.titlesize": 8,
    "xtick.labelsize": 6.8,
    "ytick.labelsize": 6.8,
    "legend.fontsize": 6.8,
    "axes.linewidth": 0.7,
    "lines.linewidth": 1.1,
    "xtick.major.size": 2.2,
    "ytick.major.size": 2.2,
    "xtick.major.pad": 1.6,
    "ytick.major.pad": 1.6,
}

ANNOT_FONTSIZE = 6.6   # >= 6.5 pt so the figure stays readable at column width
LW_CURVE = 1.15
LW_RULE = 0.9

# White outline keeps in-panel annotations legible where they cross a curve.
LABEL_HALO = path_effects.withStroke(linewidth=1.5, foreground="white")


# ---------------------------------------------------------------------------
#  Plotting
# ---------------------------------------------------------------------------

def _nice_step(span, target=4):
    """Tick step covering `span` in about `target` intervals with round labels.

    Args:
        span:   Axis range in data units (> 0).
        target: Preferred number of intervals.

    Returns:
        A 1 / 2 / 2.5 / 5 x 10^k step; slightly larger steps are preferred over
        awkward ones, so a 9,000-wide axis ticks every 2,000 rather than 2,250.
    """
    if span <= 0:
        return 1.0
    raw = span / float(target)
    exp = math.floor(math.log10(raw))
    for candidate in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = candidate * (10 ** exp)
        if step >= raw * 0.75:
            return step
    return 10.0 ** (exp + 1)


def _compact_rate(value, _pos=None):
    """Tick formatter: 8000 -> "8k", 500 -> "500" (narrow labels fit the panel).

    Args:
        value: Tick value in pods/s.
        _pos:  Tick position, ignored (matplotlib FuncFormatter signature).

    Returns:
        Formatted tick label.
    """
    if value >= 1000 and value % 1000 == 0:
        return f"{value / 1000:.0f}k"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def _cdf_points(rates, x_floor):
    """Step-CDF support points, with sub-`x_floor` rates folded onto `x_floor`.

    A log x axis cannot show windows with no arrivals at all, so their
    probability mass is kept but drawn at the left edge: the curve starts at
    (x_floor, P[rate <= x_floor]) and is exact for every rate > x_floor.

    Args:
        rates:   Window rates in pods/s (all windows of the reporting scope).
        x_floor: Left edge of the log axis in pods/s.

    Returns:
        (x, y) arrays for a `where="post"` step plot.
    """
    clipped = np.maximum(rates, x_floor)
    vals, counts = np.unique(clipped, return_counts=True)
    cum = np.cumsum(counts) / rates.size
    return vals, cum


def _draw_cdf_panel(ax, rates_by_window, cap, windows, x_floor):
    """Left panel: CDF of window arrival rates + above-cap annotation.

    Args:
        ax:              Target axes.
        rates_by_window: Arrival rate per window size, already binned by the
                         export over the reporting scope.
        cap:             Single-scheduler reference rate in pods/s.
        windows:         Window sizes to draw.
        x_floor:         Left edge of the log x axis in pods/s.

    Every window of the scope is part of the population, including empty windows
    inside it; only the quiet head and tail of the trace are excluded upstream.
    """
    rates_by_w = {w: np.asarray(rates_by_window[str(w)]) for w in windows}
    x_max = max(r.max() for r in rates_by_w.values())
    x_right = x_max * 1.7

    handles = []
    for w in windows:
        window_style = WINDOW_STYLE[w]
        rates = rates_by_w[w]
        x, y = _cdf_points(rates, x_floor)
        # Extend the last step to the right edge so the curve reaches 1.
        x = np.append(x, x_right)
        y = np.append(y, y[-1])
        ax.step(x, y, where="post", color=window_style["color"],
                ls=window_style["ls"], lw=LW_CURVE, solid_joinstyle="round",
                zorder=3)
        frac = float((rates > cap).mean())
        handles.append(mlines.Line2D(
            [], [], color=window_style["color"], ls=window_style["ls"],
            lw=LW_CURVE,
            label=f"{window_style['short']}: {frac * 100:.0f}% above"))

    ax.axvline(cap, color=C_RED_D, ls=":", lw=LW_RULE, zorder=2)
    ax.set_xscale("log")
    ax.set_xlim(x_floor, x_right)
    ax.set_ylim(0, 1.04)
    # The series expands each task row by its instance count at the task start
    # second, so the axis is a task-instance start-rate proxy, not a measured
    # per-instance (let alone per-pod) submission rate.
    ax.set_xlabel("Arrival rate (pods/s)", labelpad=1.0)
    ax.set_ylabel("CDF of windows", labelpad=1.5)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["0", ".25", ".5", ".75", "1"])
    ax.xaxis.set_major_locator(LogLocator(base=10, numticks=8))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.grid(True, which="major", ls=":", lw=0.4, alpha=0.4)

    # Cap rule label, kept above the steep part of both curves.
    ax.text(cap * 0.72, 0.955, f"{cap:g} pods/s", color=C_RED_D,
            fontsize=ANNOT_FONTSIZE, ha="right", va="top", rotation=90,
            path_effects=[LABEL_HALO], zorder=5)
    # Legend doubles as the above-cap annotation (one share per window size); it
    # is anchored at the very bottom so it stays clear of the CDF plateaus.
    legend = ax.legend(handles=handles, loc="lower right", frameon=False,
                       handlelength=1.4, handletextpad=0.35, labelspacing=0.15,
                       borderpad=0.0, borderaxespad=0.15, alignment="left")
    for text in legend.get_texts():
        text.set_path_effects([LABEL_HALO])


def _draw_window_panel(ax, series, cap, log_y):
    """Right panel: 1 s arrival series of the representative window.

    The default linear y axis is deliberate: it shows at a glance how far the
    per-second arrival rate sits above the single-scheduler reference, which a
    log axis would compress. A linear axis also represents the seconds with zero
    arrivals exactly. Under the optional log scale (`--log-window-y`) those
    zeros are drawn at the axis floor instead, since a log axis cannot show them.

    Args:
        ax:     Target axes.
        series: Per-second pod counts of the window, from the exported data.
        cap:    Single-scheduler reference rate in pods/s.
        log_y:  Use a log y axis instead of the linear default.
    """
    stats = {"mean_rate": float(series.sum()) / series.size}
    t = np.arange(series.size)

    if log_y:
        y_floor = 0.8
        y_top = series.max() * 4.2
        y = np.maximum(series, y_floor)
    else:
        y_floor = 0.0
        # Headroom for the rule legend above the peak, with the top rounded to a
        # whole 500 and ticks on a "nice" step so the labels read cleanly.
        y_top = 500.0 * math.ceil(series.max() * 1.15 / 500.0)
        y_step = _nice_step(y_top)
        y = series.astype(float)

    ax.plot(t, y, color=C_BLUE_D, lw=LW_CURVE, zorder=4)
    h_mean = ax.axhline(stats["mean_rate"], color=C_GREY, ls="--", lw=LW_RULE,
                        zorder=3, label=f"mean {stats['mean_rate']:,.0f} pods/s")
    h_cap = ax.axhline(cap, color=C_RED_D, ls=":", lw=LW_RULE, zorder=3,
                       label=f"{cap:g} pods/s")

    ax.set_xlim(-0.6, series.size - 0.4)
    ax.set_xticks([0, 20, 40, 60])
    # The panel sits at the right edge of a single-column figure, so a label
    # wide enough to also name the window start (t=627,120) overflows the saved
    # tight bounding box. The absolute start second stays in the stats JSON and
    # in the window's own first second rather than on the axis.
    ax.set_xlabel("Trace time (s)", labelpad=1.0)
    ax.set_ylabel("Arrival rate (pods/s)", labelpad=1.5)
    ax.grid(True, which="major", ls=":", lw=0.4, alpha=0.4)

    if log_y:
        ax.set_yscale("log")
        ax.set_ylim(y_floor, y_top)
        ax.yaxis.set_major_locator(LogLocator(base=10, numticks=6))
        ax.yaxis.set_minor_formatter(NullFormatter())
        # On a log axis both rules are far apart, so label them in place.
        ax.text(0.5, stats["mean_rate"] * 1.35,
                f"mean {stats['mean_rate']:,.0f}/s",
                color="#4d4d4d", fontsize=ANNOT_FONTSIZE, ha="left", va="bottom",
                path_effects=[LABEL_HALO], zorder=5)
        ax.text(series.size - 1.0, cap / 1.30,
                f"{cap:g}/s scheduler", color=C_RED_D,
                fontsize=ANNOT_FONTSIZE, ha="right", va="top",
                path_effects=[LABEL_HALO], zorder=5)
    else:
        ax.set_ylim(0, y_top)
        ax.yaxis.set_major_locator(MultipleLocator(y_step))
        ax.yaxis.set_major_formatter(FuncFormatter(_compact_rate))
        # The cap sits close to the x axis here, so in-place labels would
        # collide with it and with the curve; a frameless legend in the empty
        # upper-right corner keeps both rules identifiable.
        legend = ax.legend(handles=[h_mean, h_cap], loc="upper right",
                           frameon=False, handlelength=1.3, handletextpad=0.35,
                           labelspacing=0.12, borderpad=0.0, borderaxespad=0.15,
                           alignment="left")
        for text, colour in zip(legend.get_texts(), ("#4d4d4d", C_RED_D)):
            text.set_color(colour)
            text.set_path_effects([LABEL_HALO])
    return stats


def build_figure(*, cdf_rates, window_series, cap, windows, width, height,
                 x_floor, log_y, panel_titles, cdf_title, cdf_only=False):
    """Assemble the single-column figure (CDF panel, optionally + window panel).

    Args:
        cdf_rates:     Arrival rate per window size, binned by the export.
        window_series: Per-second arrivals of the representative window.
        cap:           Single-scheduler reference rate in pods/s.
        windows:       Window sizes drawn in the CDF panel.
        width:         Figure width in inches (3.33 == sigplan \columnwidth).
        height:        Figure height in inches.
        x_floor:       Left edge of the CDF log axis in pods/s.
        log_y:         Whether the time-series panel uses a log y axis.
        panel_titles:  Whether to print the "(a)"/"(b)" panel titles.
        cdf_title:     Title of the CDF panel, naming its reporting scope.
        cdf_only:      Draw only the CDF panel at full figure width. Useful for
                       diagnostics; the paper uses the two-panel layout so
                       readers can see a representative burst window.

    Returns:
        The matplotlib Figure.
    """
    style.apply(**RC_OVERRIDES)

    fig = plt.figure(figsize=(width, height))
    top = 1.0 - (0.20 if panel_titles else 0.06) / height
    n_cols = 1 if cdf_only else 2
    gs = GridSpec(1, n_cols, figure=fig, wspace=0.52,
                  left=0.135 if not cdf_only else 0.125,
                  right=0.985, top=top, bottom=0.30 / height)
    ax_cdf = fig.add_subplot(gs[0, 0])

    _draw_cdf_panel(ax_cdf, cdf_rates, cap, windows, x_floor)
    if not cdf_only:
        ax_win = fig.add_subplot(gs[0, 1])
        _draw_window_panel(ax_win, window_series, cap, log_y)

    if panel_titles:
        # Regular weight, matching the panel titles of the other paper figures.
        ax_cdf.set_title(cdf_title, fontsize=RC_OVERRIDES["axes.titlesize"],
                         loc="left", pad=2)
        if not cdf_only:
            ax_win.set_title("(b) 60 s window",
                             fontsize=RC_OVERRIDES["axes.titlesize"],
                             loc="left", pad=2)
    return fig



# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=envelope.path_for(_EXPERIMENTS, FIGURE),
                        help="Exported figure data (default: the export path)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--width", type=float, default=DEFAULT_WIDTH,
                        help="Figure width in inches (3.33 = sigplan column width)")
    parser.add_argument("--height", type=float, default=DEFAULT_HEIGHT)
    parser.add_argument("--cdf-x-floor", type=float, default=DEFAULT_CDF_X_FLOOR,
                        help="Left edge of the CDF log axis in pods/s")
    parser.add_argument("--log-window-y", action="store_true",
                        help="Use a log y axis on the window panel")
    parser.add_argument("--no-panel-titles", action="store_true",
                        help="Omit the (a)/(b) panel titles")
    parser.add_argument("--cdf-only", action="store_true",
                        help="Draw only the CDF panel, at full figure width")
    parser.add_argument("--show", action="store_true",
                        help="Display the figure after saving")
    args = parser.parse_args(argv)

    data = envelope.load(args.data, figure=FIGURE)
    figure = build_figure(
        cdf_rates=data["cdf_rates"],
        window_series=np.asarray(data["window"]["series"]),
        cap=data["cap"],
        windows=data["cdf_windows"],
        width=args.width,
        height=args.height,
        x_floor=args.cdf_x_floor,
        log_y=args.log_window_y,
        panel_titles=not args.no_panel_titles,
        cdf_title=data["cdf_title"],
        cdf_only=args.cdf_only,
    )
    for path in style.save_figure(figure, args.output_dir, FIGURE,
                                  dpi=400, pad_inches=0.012):
        print(path)

    if args.show:
        plt.show()
    else:
        plt.close(figure)
    return 0


if __name__ == "__main__":
    sys.exit(main())
