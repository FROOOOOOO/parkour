"""Experiment matrix for Figure 4 conflict decomposition."""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import FigureCase, make_case
from config import ModelConstants, SimulationConfig

MATRIX_KIND = "fig4-conflict-v1"
SCHEDULER_COUNTS = (2, 5, 10, 20)
TIER_WIDTHS = (400, 1_000, 2_000, 20_000)
GAP_CYCLES = (None, 5, 10, 25, 50)

RAW_CACHE = FIGURE_DIR / "data" / "results.json"
VERIFIED_CACHE = FIGURE_DIR / "data" / "verified.json"
MANIFEST = FIGURE_DIR / "data" / "manifest.json"
REPORT = FIGURE_DIR / "data" / "verification.md"
OUTPUT_DIR = FIGURE_DIR / "output"


def constants() -> ModelConstants:
    """Return the Figure 4 workload shared by both columns.

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


def _config(num_tiers: int, gap_cycles: int | None) -> SimulationConfig:
    config = SimulationConfig(
        num_tiers=num_tiers,
        sync_mode="event" if gap_cycles is None else "periodic",
        sync_gap_cycles=gap_cycles,
        num_backup=0,
        list_mode="fixed",
        score_threshold=0.0,
        penalty_weight=0.0,
        penalty_scope="shared",
        penalty_noise=0.0,
        publish_gap_cycles=10,
        trial_seed=0,
    )
    config.validate(constants())
    return config


def cases() -> list[FigureCase]:
    """Return the union of the m sweep and tier-width sweep."""

    points = (
        {(num_schedulers, 10) for num_schedulers in SCHEDULER_COUNTS}
        | {
            (10, constants().num_nodes // width)
            for width in TIER_WIDTHS
        }
    )
    rows = [
        make_case(_config(num_tiers, gap), num_schedulers)
        for num_schedulers, num_tiers in sorted(points)
        for gap in GAP_CYCLES
    ]
    if len(rows) != 35 or len({case.case_id for case in rows}) != 35:
        raise AssertionError("Figure 4 must contain 35 unique cases")
    return rows
