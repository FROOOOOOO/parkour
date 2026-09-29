"""Shared figure style for the cluster-experiment figures.

This module owns the parts of the style that must not differ between figures:
the font family, the mathtext faces, and the font-embedding settings that the
publisher requires. Per-figure sizes (font sizes, line widths, marker sizes)
are deliberately *not* here, because figures occupy different column widths and
legitimately need different sizes; pass them to `apply()` as overrides.

The equivalent module on the simulation side is `simulation/common/plotting.py`.
`FONT_PARAMS` is kept identical between the two so that simulation figures and
cluster figures render in the same family; the two trees stay importable on
their own, which is why the block is duplicated rather than shared.
"""

from __future__ import annotations

import os
from typing import Any

import matplotlib.pyplot as plt
import seaborn as sns

# Arial for text and mathtext alike. Left alone, the family arrives only as a
# side effect of seaborn's style dict and mathtext keeps its own DejaVu set, so
# symbols such as $K$ render in a different face to the surrounding axes.
FONT_PARAMS: dict[str, Any] = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "mathtext.fontset": "custom",
    "mathtext.rm": "Arial",
    "mathtext.it": "Arial:italic",
    "mathtext.bf": "Arial:bold",
}

# Type 42 embeds the font outlines in PDF/PS; "none" keeps SVG text as text
# rather than converting it to paths, so the SVG stays editable and searchable.
EMBED_PARAMS: dict[str, Any] = {
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
}

BASE_RC: dict[str, Any] = {**EMBED_PARAMS, **FONT_PARAMS}

# Colours used by more than one figure. Single-use colours stay in the figure
# that uses them: hoisting them here would suggest a shared meaning they do not
# have.
#
# Across the figures that compare systems, the convention is one hue per system
# and a dark/light pair per paradigm: dark for event-driven, light for periodic.
PARKOUR_GREEN = "#238b45"        # ParKour, event-driven
PARKOUR_GREEN_LIGHT = "#74c476"  # ParKour, periodic
VANILLA_RED = "#c94641"          # Vanilla, event-driven
VANILLA_RED_LIGHT = "#f7b698"    # Vanilla, periodic
GODEL_PURPLE = "#A78AB8"         # Godel baseline (static partitioning)
EVENT_BLUE = "#4575b4"           # event-driven paradigm, where no system is compared
PERIODIC_ORANGE = "#fd8d3c"      # periodic paradigm, paired with EVENT_BLUE
REFERENCE_BLUE = "#3783ba"       # reference rules and annotations
EDGE_GREY = "#333333"            # bar and marker edges
NEUTRAL_GREY = "#999999"         # de-emphasized reference series

# The red and blue entries above are frozen samples of seaborn's "RdBu" palette
# at n=11 (indices 1, 3 and 9), which four figures previously each re-derived at
# import time. They are written out so that a change in seaborn's colormaps
# cannot silently move the colours of already published figures.

GRID_KW: dict[str, Any] = {"linestyle": "--", "alpha": 0.42, "linewidth": 0.55}

OUTPUT_FORMATS = ("pdf", "svg", "png")


def apply(
    *,
    context: str | None = None,
    font_scale: float = 1.0,
    **overrides: Any,
) -> None:
    """Install the shared style, then the caller's per-figure overrides.

    The steps are ordered. Both seaborn's style dict and its context dict carry
    font settings of their own, so they have to run before the rcParams update
    or the shared family is silently discarded and mathtext falls back to
    DejaVu. Keeping the order here means no figure can get it wrong.

    `context` is seaborn's scaling preset ("paper", "talk", ...). Most figures
    set their sizes explicitly through `overrides` and leave it unset.
    """

    sns.set_style("ticks")
    if context is not None:
        sns.set_context(context, font_scale=font_scale)
    params = dict(BASE_RC)
    params.update(overrides)
    plt.rcParams.update(params)


def save_figure(figure, output_dir: str, stem: str, **savefig_kw: Any) -> list[str]:
    """Write `stem`.{pdf,svg,png} into `output_dir` and return the paths.

    `savefig_kw` is passed through to matplotlib, so a figure can set its own
    padding or hand over `bbox_extra_artists` for legends placed outside the
    axes, which would otherwise be clipped out of the tight bounding box.
    """

    os.makedirs(output_dir, exist_ok=True)
    defaults: dict[str, Any] = {"bbox_inches": "tight", "pad_inches": 0.01}
    raster_dpi = savefig_kw.pop("dpi", 300)
    defaults.update(savefig_kw)

    paths = []
    for extension in OUTPUT_FORMATS:
        path = os.path.join(output_dir, f"{stem}.{extension}")
        figure.savefig(
            path, dpi=raster_dpi if extension == "png" else None, **defaults
        )
        paths.append(path)
    return paths
