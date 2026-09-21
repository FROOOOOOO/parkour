"""Experiment matrix for Figure 7 penalty and fallback-list interaction."""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import FigureCase, make_case
from config import ModelConstants, SimulationConfig

MATRIX_KIND = "fig7-penalty-v3"
GAP_CYCLES = (None, 10, 25, 50)
PENALTY_WEIGHTS = (0.0, 0.3, 0.5, 0.7)
EVENT_PUBLISH_GAP_CYCLES = 1
PERIODIC_PUBLISH_GAP_CYCLES = 10

RAW_CACHE = FIGURE_DIR / "data" / "results.json"
VERIFIED_CACHE = FIGURE_DIR / "data" / "verified.json"
MANIFEST = FIGURE_DIR / "data" / "manifest.json"
REPORT = FIGURE_DIR / "data" / "verification.md"
OUTPUT_DIR = FIGURE_DIR / "output"


def constants() -> ModelConstants:
    """Return the Figure 7 workload.

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
    num_backup: int,
    weight: float,
) -> SimulationConfig:
    """Create one shared-signal penalty case.

    Event mode publishes on every simulation cycle, the smallest causal cadence:
    binding outcomes of cycle t become available before selection in cycle t+1.
    Periodic availability cases publish penalty statistics every second.
    """

    config = SimulationConfig(
        num_tiers=10,
        sync_mode="event" if gap_cycles is None else "periodic",
        sync_gap_cycles=gap_cycles,
        num_backup=num_backup,
        list_mode="fixed",
        score_threshold=0.0,
        penalty_weight=weight,
        penalty_scope="shared",
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
    """Return the union of penalty-only and K-plus-penalty comparisons."""

    configs = {
        _config(
            gap_cycles=gap,
            num_backup=0,
            weight=weight,
        )
        for gap in GAP_CYCLES
        for weight in PENALTY_WEIGHTS
    }
    configs.update(
        _config(
            gap_cycles=gap,
            num_backup=2,
            weight=weight,
        )
        for gap in GAP_CYCLES
        # 0.5 is the deployed feedback rate and is what the centre panel plots;
        # 0.3 is retained so the earlier reported figures stay reproducible.
        for weight in (0.0, 0.3, 0.5)
    )
    rows = [
        make_case(config, constants().num_schedulers)
        for config in sorted(
            configs,
            key=lambda item: (
                item.sync_gap_cycles or 0,
                item.num_backup,
                item.penalty_weight,
            ),
        )
    ]
    # 4 gaps x 4 weights with no fallback, plus 4 gaps x 3 weights with it.
    expected = len(GAP_CYCLES) * (len(PENALTY_WEIGHTS) + 3)
    if len(rows) != expected or len({case.case_id for case in rows}) != expected:
        raise AssertionError(f"Figure 7 must contain {expected} unique cases")
    return rows
