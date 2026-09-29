"""Drawing helpers shared by more than one figure.

Two kinds of thing live here. The first is mechanical: turning the exported
median/Q1/Q3 bands into what matplotlib's `yerr` wants, and the bar styling that
every bar chart in this set uses. The second is vocabulary shared by figures that
were split out of a single script and must keep naming their categories the same
way, which is why it is here rather than duplicated in each of them.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np

from . import style

#: Bar edges and error-bar caps, identical across the bar charts.
BAR_EDGE = {"edgecolor": "black", "linewidth": 0.4}
ERROR_KW = {"elinewidth": 0.7, "capsize": 2.0, "ecolor": "#333333"}


def bar_kwargs(**overrides: Any) -> dict[str, Any]:
    """Keyword arguments for `Axes.bar`, with the shared edge and error style."""

    kwargs: dict[str, Any] = {**BAR_EDGE, "error_kw": dict(ERROR_KW)}
    kwargs.update(overrides)
    return kwargs


def iqr_error(bands: Sequence[dict[str, float | None]]) -> np.ndarray:
    """Convert exported {median, q1, q3} bands to a (2, n) asymmetric error array.

    Negative lengths are clamped to zero: a band can be degenerate when a cell
    kept a single trial, and matplotlib rejects negative error lengths.
    """

    lower, upper = [], []
    for band in bands:
        median = band.get("median")
        if median is None:
            lower.append(0.0)
            upper.append(0.0)
            continue
        q1 = band.get("q1")
        q3 = band.get("q3")
        lower.append(max(0.0, median - (median if q1 is None else q1)))
        upper.append(max(0.0, (median if q3 is None else q3) - median))
    return np.array([lower, upper])


def medians(bands: Iterable[dict[str, float | None]]) -> list[float]:
    """Medians of a band sequence, with missing cells drawn as zero-height."""

    return [0.0 if band.get("median") is None else float(band["median"])
            for band in bands]


# ---------------------------------------------------------------------------
#  Ablation vocabulary
#
#  The ablation board contributes two paper figures that were once one script.
#  They have to label the same four configurations identically and use the same
#  colour per paradigm, so both read those from here.
# ---------------------------------------------------------------------------

CONFIG_LABELS = {
    "vanilla": "Vanilla",
    "multicandidate": "Multi-candidate\nonly",
    "penalty": "Penalty\nonly",
    "parkour": "ParKour",
}

C_EVENT = style.EVENT_BLUE
C_PERIODIC = style.PERIODIC_ORANGE
