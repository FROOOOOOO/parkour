"""Experiment matrix for Figure 5 multi-candidate behavior."""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import FigureCase, make_case
from config import ModelConstants, SimulationConfig

MATRIX_KIND = "fig5-multicandidate-v1"
FALLBACK_BUDGETS = (0, 1, 2, 4, 8)
GAP_CYCLES = (None, 5, 10, 25, 50)

RAW_CACHE = FIGURE_DIR / "data" / "results.json"
VERIFIED_CACHE = FIGURE_DIR / "data" / "verified.json"
MANIFEST = FIGURE_DIR / "data" / "manifest.json"
REPORT = FIGURE_DIR / "data" / "verification.md"
OUTPUT_DIR = FIGURE_DIR / "output"


def constants() -> ModelConstants:
    """Return the Figure 5 workload.

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


def _config(fallbacks: int, gap_cycles: int | None) -> SimulationConfig:
    config = SimulationConfig(
        num_tiers=10,
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
    rows = [
        make_case(_config(fallbacks, gap), constants().num_schedulers)
        for fallbacks in FALLBACK_BUDGETS
        for gap in GAP_CYCLES
    ]
    if len(rows) != 25 or len({case.case_id for case in rows}) != 25:
        raise AssertionError("Figure 5 must contain 25 unique cases")
    return rows
