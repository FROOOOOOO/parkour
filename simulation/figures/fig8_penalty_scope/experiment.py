"""Experiment matrix for Figure 8 penalty information scope."""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import FigureCase, make_case
from config import ModelConstants, SimulationConfig

MATRIX_KIND = "fig8-penalty-scope-v2"
GAP_CYCLES = (None, 10, 25, 50)
# The non-zero weight is the deployed feedback rate. Here it is a fixed
# operating point for the scope comparison, not a swept variable.
METHODS = (
    ("baseline", 0.0, "shared"),
    ("shared", 0.5, "shared"),
    ("local", 0.5, "local"),
)
EVENT_PUBLISH_GAP_CYCLES = 1
PERIODIC_PUBLISH_GAP_CYCLES = 10

RAW_CACHE = FIGURE_DIR / "data" / "results.json"
VERIFIED_CACHE = FIGURE_DIR / "data" / "verified.json"
MANIFEST = FIGURE_DIR / "data" / "manifest.json"
REPORT = FIGURE_DIR / "data" / "verification.md"
OUTPUT_DIR = FIGURE_DIR / "output"


def constants() -> ModelConstants:
    """Return the workload shared by all Figure 8 methods.

    Injection runs for the whole guard window so submission never stops once
    cumulative arrivals reach the node count, and the rate exceeds one
    scheduler's per-cycle attempt cap so the arrival stream cannot be absorbed
    without parallelism.
    """

    return ModelConstants(
        num_nodes=20_000,
        num_schedulers=10,
        cycle_seconds=0.1,
        scheduler_capacity_per_cycle=40,
        arrivals_per_cycle=100,
        injection_cycles=5_000,
        max_cycles=5_000,
        node_capacity=1,
        score_seed=0,
        score_sigma=0.5,
    )


def _config(
    *,
    gap_cycles: int | None,
    weight: float,
    scope: str,
) -> SimulationConfig:
    """Create one K=0 penalty-scope comparison case."""

    config = SimulationConfig(
        num_tiers=10,
        sync_mode="event" if gap_cycles is None else "periodic",
        sync_gap_cycles=gap_cycles,
        num_backup=0,
        list_mode="fixed",
        score_threshold=0.0,
        penalty_weight=weight,
        penalty_scope=scope,
        penalty_noise=0.0,
        publish_gap_cycles=(
            EVENT_PUBLISH_GAP_CYCLES
            if gap_cycles is None
            else PERIODIC_PUBLISH_GAP_CYCLES
        ),
        trial_seed=0,
    )
    config.validate(constants())
    return config


def cases() -> list[FigureCase]:
    """Return all information-scope methods over the common sync-gap sweep."""

    rows = [
        make_case(
            _config(gap_cycles=gap, weight=weight, scope=scope),
            constants().num_schedulers,
        )
        for _, weight, scope in METHODS
        for gap in GAP_CYCLES
    ]
    if len(rows) != 12 or len({case.case_id for case in rows}) != 12:
        raise AssertionError("Figure 8 must contain 12 unique cases")
    return rows
