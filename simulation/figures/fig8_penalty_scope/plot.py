#!/usr/bin/env python3
"""Render Figure 8, total conflict rate by penalty information scope.

The panel itself is drawn by `panels.py`, which Figure 7 also reuses.
"""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

from common.plotting import save_figure
from common.validation import load_verified
from figures.fig8_penalty_scope.experiment import MANIFEST, OUTPUT_DIR, VERIFIED_CACHE
from figures.fig8_penalty_scope.panels import FIG8_STYLE, draw_legend, draw_panel


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
