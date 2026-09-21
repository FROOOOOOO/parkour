"""Shared paper styling and aggregation helpers."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import matplotlib as mpl
import numpy as np

# Arial for body text and mathtext alike, spread into every figure package's
# style dict so the paper's figures agree on one family.
#
# This has to be declared rather than assumed. The packages that already
# rendered in Arial only did so as a side effect of ``sns.set_style()``, which
# puts Arial first in ``font.sans-serif``; the packages that never import
# seaborn fell back to matplotlib's DejaVu Sans without any warning. Mathtext
# carries its own font set on top of ``font.family``, so it needs pointing at
# Arial separately or labels such as $G$, $K$ and $w$ come out in DejaVu next
# to Arial axis text.
FONT_PARAMS = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "mathtext.fontset": "custom",
    "mathtext.rm": "Arial",
    "mathtext.it": "Arial:italic",
    "mathtext.bf": "Arial:bold",
}

RC_PARAMS = {
    "font.size": 7.2,
    "axes.labelsize": 7.2,
    "axes.titlesize": 7.2,
    "xtick.labelsize": 6.8,
    "ytick.labelsize": 6.8,
    "legend.fontsize": 6.5,
    "legend.title_fontsize": 6.8,
    "axes.linewidth": 0.7,
    "lines.linewidth": 1.3,
    "lines.markersize": 3.4,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
    **FONT_PARAMS,
}

COLOR_BLUE = "#2166ac"
COLOR_BLUE_LIGHT = "#92c5de"
COLOR_RED = "#b2182b"
COLOR_RED_LIGHT = "#f4a582"
COLOR_GRAY = "#666666"
COLOR_GRAY_LIGHT = "#999999"
GRID = {"linestyle": "--", "alpha": 0.42, "linewidth": 0.55}
GAPS_SECONDS = (0.0, 0.5, 1.0, 2.5, 5.0)
PERIODIC_GAPS_SECONDS = GAPS_SECONDS[1:]


def apply_paper_style() -> None:
    """Activate the shared single-column paper style."""

    mpl.rcParams.update(RC_PARAMS)


def mean_std(values: Iterable[float]) -> tuple[float, float, int]:
    """Return population mean, standard deviation, and support."""

    array = np.asarray(list(values), dtype=float)
    if array.size == 0 or not np.all(np.isfinite(array)):
        raise ValueError("cannot aggregate empty or non-finite values")
    return float(array.mean()), float(array.std(ddof=0)), int(array.size)


def group_runs(data: dict) -> dict[str, list[dict]]:
    """Index a figure cache by case id."""

    grouped: dict[str, list[dict]] = defaultdict(list)
    for run in data["runs"]:
        grouped[run["case_id"]].append(run)
    return grouped


def sync_gap_seconds(run: dict, cycle_seconds: float) -> float:
    """Map event mode to zero and periodic gaps to seconds."""

    config = run["config"]
    if config["sync_mode"] == "event":
        return 0.0
    return float(config["sync_gap_cycles"]) * cycle_seconds


def configure_axis(axis: Any, *, percent: bool = True) -> None:
    """Apply restrained paper cosmetics."""

    from matplotlib.ticker import PercentFormatter

    axis.grid(True, axis="y", **GRID)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    if percent:
        axis.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))


def set_gap_ticks(axis: Any) -> None:
    """Use the common event/periodic gap axis."""

    axis.set_xticks(GAPS_SECONDS, ["event", "0.5", "1", "2.5", "5"])
    axis.tick_params(axis="x", labelrotation=28)
    for label in axis.get_xticklabels():
        label.set_horizontalalignment("right")


def panel_title(axis: Any, letter: str, text: str) -> None:
    """Draw a concise left-aligned panel title."""

    axis.set_title(f"({letter}) {text}", loc="left", fontweight="bold", pad=3)


def save_figure(
    figure: Any,
    output_dir: Path,
    stem: str,
    *,
    pad_inches: float = 0.01,
) -> list[Path]:
    """Save PDF, SVG, and PNG variants."""

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for extension in ("pdf", "svg", "png"):
        path = output_dir / f"{stem}.{extension}"
        figure.savefig(
            path,
            dpi=300,
            bbox_inches="tight",
            pad_inches=pad_inches,
        )
        paths.append(path)
    return paths
