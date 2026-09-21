"""Experiment matrix for Figure 6 fixed and variable-list binder costs."""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import FigureCase, make_case
from config import ModelConstants, SimulationConfig

MATRIX_KIND = "fig6-binder-cost-v2"
DESIGNS = ("k0", "k8", "threshold")
GAP_CYCLES = (None, 10, 25, 50)
SETTINGS = ((10, 2_000), (20, 400))

RAW_CACHE = FIGURE_DIR / "data" / "results.json"
VERIFIED_CACHE = FIGURE_DIR / "data" / "verified.json"
MANIFEST = FIGURE_DIR / "data" / "manifest.json"
REPORT = FIGURE_DIR / "data" / "verification.md"
OUTPUT_DIR = FIGURE_DIR / "output"


def constants() -> ModelConstants:
    return ModelConstants(
        num_nodes=20_000,
        num_schedulers=10,
        cycle_seconds=0.1,
        scheduler_capacity_per_cycle=40,
        arrivals_per_cycle=100,
        injection_cycles=3_000,
        max_cycles=3_000,
        node_capacity=1,
        score_seed=0,
        score_sigma=0.5,
    )


def _config(
    design: str,
    gap_cycles: int | None,
    tier_width: int,
) -> SimulationConfig:
    """Create one list design at a fixed synchronization gap and tier width."""

    if design not in DESIGNS:
        raise ValueError(f"unsupported Figure 6 design: {design}")
    config = SimulationConfig(
        num_tiers=constants().num_nodes // tier_width,
        sync_mode="event" if gap_cycles is None else "periodic",
        sync_gap_cycles=gap_cycles,
        num_backup=0 if design == "k0" else 8,
        list_mode="threshold" if design == "threshold" else "fixed",
        score_threshold=0.1 if design == "threshold" else 0.0,
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
        make_case(_config(design, gap, width), schedulers)
        for schedulers, width in SETTINGS
        for design in DESIGNS
        for gap in GAP_CYCLES
    ]
    if len(rows) != 24 or len({case.case_id for case in rows}) != 24:
        raise AssertionError("Figure 6 must contain 24 unique cases")
    return rows
