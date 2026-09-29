"""The trial-level outlier filter shared by every board.

This rule decides which trials contribute to a published number, so it has to
have exactly one implementation. It previously existed in four places
(`apply-outlier-filter.py`, and one private copy inside each of three figure
scripts); the copies were semantically equivalent but written differently, so a
change to one of them would have silently moved a published figure away from the
others.

A trial is dropped when any of the following holds:

1.  It recorded no scheduled pods, so it did not run.
2.  It is a *metrics hole*: the run completed (>=90% of the expected pods were
    scheduled) but Prometheus reported neither ACF nor bind conflicts, while at
    least two other trials in the same cell recorded a bind-conflict rate above
    1%. Requiring two witnesses avoids flagging cells that are legitimately
    conflict-free, such as single-scheduler runs.
3.  Its scheduling duration exceeds BOTH the Tukey fence `Q3 + 1.5*IQR` and a
    magnitude floor of `2 * median`. The second condition protects cells whose
    IQR is narrow because every trial clustered tightly.

Rule 3 is skipped for cells with fewer than four trials, where quartiles are not
meaningful. Filtering on duration rather than on throughput avoids selecting for
the outcome being measured: a stalled trial (retry loop, KWOK hiccup, warm-up)
inflates duration disproportionately.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Sequence

Trial = dict[str, Any]

#: Bumped when the rule changes, and recorded in exported data so a figure can
#: be traced back to the filter that produced it.
FILTER_VERSION = "duration-outlier-v1"

MIN_TRIALS_FOR_IQR = 4
HOLE_COMPLETION_RATIO = 0.9
HOLE_EVIDENCE_RATE = 0.01
HOLE_MIN_WITNESSES = 2
TUKEY_MULTIPLIER = 1.5
MEDIAN_FLOOR_MULTIPLIER = 2.0


def as_float(value: Any) -> float | None:
    """Parse a CSV cell or JSON value to float, returning None when unusable."""

    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def median_iqr(values: Sequence[float]) -> tuple[float, float, float]:
    """Return (median, Q1, Q3) using the same quantile convention throughout."""

    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        raise ValueError("median_iqr needs at least one value")

    def quantile(fraction: float) -> float:
        position = fraction * (count - 1)
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return statistics.median(ordered), quantile(0.25), quantile(0.75)


def _is_metrics_hole(trial: Trial, cell_has_conflict: bool) -> bool:
    scheduled = as_float(trial.get("scheduled_pods"))
    expected = as_float(trial.get("expected_pods")) or scheduled
    acf_count = as_float(trial.get("acf_count"))
    bind_count = as_float(trial.get("bind_conflict_count"))
    return bool(
        cell_has_conflict
        and acf_count is not None
        and acf_count == 0
        and bind_count is not None
        and bind_count == 0
        and expected
        and expected > 0
        and scheduled is not None
        and scheduled / expected >= HOLE_COMPLETION_RATIO
    )


def filter_cell(trials: Iterable[Trial]) -> tuple[list[Trial], list[tuple[str, str]]]:
    """Filter one cell's trials.

    A *cell* is one experiment configuration; the metrics-hole rule needs the
    whole cell because it reasons about sibling trials.

    Returns the kept trials and a list of (trial label, reason) for the drops.
    """

    dropped: list[tuple[str, str]] = []

    running = []
    for trial in trials:
        scheduled = as_float(trial.get("scheduled_pods"))
        if scheduled is None or scheduled <= 0:
            dropped.append((str(trial.get("trial", "?")), "no scheduled pods"))
            continue
        running.append(trial)

    if not running:
        return [], dropped

    witnesses = sum(
        1
        for trial in running
        if (as_float(trial.get("bind_conflict_rate")) or 0.0) > HOLE_EVIDENCE_RATE
    )
    cell_has_conflict = witnesses >= HOLE_MIN_WITNESSES

    intact = []
    for trial in running:
        if _is_metrics_hole(trial, cell_has_conflict):
            dropped.append(
                (str(trial.get("trial", "?")), "metrics hole (acf=bind=0 mid-cell)")
            )
        else:
            intact.append(trial)

    if len(intact) < MIN_TRIALS_FOR_IQR:
        return intact, dropped

    durations = [as_float(trial.get("scheduling_duration_s")) for trial in intact]
    usable = [value for value in durations if value is not None]
    if len(usable) < MIN_TRIALS_FOR_IQR:
        return intact, dropped

    median, q1, q3 = median_iqr(usable)
    upper = max(q3 + TUKEY_MULTIPLIER * (q3 - q1), MEDIAN_FLOOR_MULTIPLIER * median)

    kept = []
    for trial, duration in zip(intact, durations):
        if duration is not None and duration > upper:
            dropped.append(
                (str(trial.get("trial", "?")), f"duration {duration:.1f}s > {upper:.1f}s")
            )
        else:
            kept.append(trial)
    return kept, dropped


def filter_trials(trials: Iterable[Trial]) -> list[Trial]:
    """`filter_cell` without the drop log, for callers that only plot."""

    kept, _ = filter_cell(trials)
    return kept


def median_of(trials: Iterable[Trial], field: str) -> float | None:
    """Median of one field over trials, ignoring unusable values."""

    values = [as_float(trial.get(field)) for trial in trials]
    usable = [value for value in values if value is not None]
    return statistics.median(usable) if usable else None


def median_iqr_of(
    trials: Iterable[Trial], field: str
) -> tuple[float, float, float] | None:
    """(median, Q1, Q3) of one field over trials, or None when no value is usable."""

    values = [as_float(trial.get(field)) for trial in trials]
    usable = [value for value in values if value is not None]
    return median_iqr(usable) if usable else None
