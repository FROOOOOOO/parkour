"""Full m-by-tier-width robustness matrix for Figure 5."""

from __future__ import annotations

import sys
from pathlib import Path

STUDY_DIR = Path(__file__).resolve().parent
SIM_ROOT = STUDY_DIR.parents[2]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import FigureCase, make_case
from config import ModelConstants, SimulationConfig

MATRIX_KIND = "fig5-multicandidate-robustness-v1"
SCHEDULER_COUNTS = (2, 5, 10, 20)
TIER_WIDTHS = (400, 1_000, 2_000, 20_000)
FALLBACK_BUDGETS = (0, 1, 2, 4)
GAP_CYCLES = (None, 5, 10, 25, 50)

RAW_CACHE = STUDY_DIR / "data" / "results.json"
VERIFIED_CACHE = STUDY_DIR / "data" / "verified.json"
MANIFEST = STUDY_DIR / "data" / "manifest.json"
REPORT = STUDY_DIR / "data" / "verification.md"
ANALYSIS = STUDY_DIR / "data" / "analysis.md"
ANALYSIS_JSON = STUDY_DIR / "data" / "analysis.json"


def constants() -> ModelConstants:
    """Return the workload held fixed across the robustness matrix.

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
    tier_width: int,
    fallbacks: int,
    gap_cycles: int | None,
) -> SimulationConfig:
    config = SimulationConfig(
        num_tiers=constants().num_nodes // tier_width,
        sync_mode="event" if gap_cycles is None else "periodic",
        sync_gap_cycles=gap_cycles,
        num_backup=fallbacks,
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
    """Return the complete m x W x K x G matrix."""

    rows = [
        make_case(
            _config(
                tier_width=tier_width,
                fallbacks=fallbacks,
                gap_cycles=gap,
            ),
            schedulers,
        )
        for schedulers in SCHEDULER_COUNTS
        for tier_width in TIER_WIDTHS
        for fallbacks in FALLBACK_BUDGETS
        for gap in GAP_CYCLES
    ]
    if len(rows) != 320 or len({case.case_id for case in rows}) != 320:
        raise AssertionError("robustness matrix must contain 320 unique cases")
    return rows
