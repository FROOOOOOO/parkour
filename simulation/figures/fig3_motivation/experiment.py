"""Experiment matrix for Figure 3 naive-parallelism motivation.

The figure answers the Section 2 motivation question: does adding schedulers
convert into placement progress, and how much of the loss is recoverable by a
bounded fallback list alone? It therefore sweeps the scheduler count at the
default tier width under two fallback budgets and three synchronization
settings, and deliberately holds the penalty weight at zero so that Section 2
makes no claim the penalty study in Section 3.4 has not yet established.

Every constant here matches the other paper packages, so the zero-backup arms are
expected to reproduce the Figure 4 and Figure 5 caches bit for bit at the
shared work points; ``verify.py`` checks that explicitly.

Naming note: this package counts *backups*, while the paper counts the whole
candidate list, so ``num_backup = K - 1``. The zero-backup arm is the paper's
K=1 (native single-target scheduling) and the deployed arm is K=3. These names
are part of the hash-locked case registry and must not be renamed to match the
paper; the mapping is applied where the figures are drawn.
"""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import FigureCase, make_case
from config import ModelConstants, SimulationConfig

MATRIX_KIND = "fig3-motivation-v1"
SCHEDULER_COUNTS = (2, 5, 10, 20)
# (num_backup, penalty_weight): the vanilla baseline, the fallback list alone,
# and the configuration deployed on the cluster.
SYSTEMS = ((0, 0.0), (2, 0.0), (2, 0.5))
GAP_CYCLES = (None, 10, 50)
TIER_WIDTH = 2_000
EVENT_PUBLISH_GAP_CYCLES = 1
PERIODIC_PUBLISH_GAP_CYCLES = 10

RAW_CACHE = FIGURE_DIR / "data" / "results.json"
VERIFIED_CACHE = FIGURE_DIR / "data" / "verified.json"
MANIFEST = FIGURE_DIR / "data" / "manifest.json"
REPORT = FIGURE_DIR / "data" / "verification.md"
OUTPUT_DIR = FIGURE_DIR / "output"


def constants() -> ModelConstants:
    """Return the Figure 3 workload, identical to the other paper packages.

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
    *, gap_cycles: int | None, num_backup: int, weight: float
) -> SimulationConfig:
    """Create one scheduler-count sweep case at a fixed tier width."""

    config = SimulationConfig(
        num_tiers=constants().num_nodes // TIER_WIDTH,
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
    """Return the scheduler-count sweep crossed with budget and sync gap."""

    rows = [
        make_case(
            _config(gap_cycles=gap, num_backup=num_backup, weight=weight),
            num_schedulers,
        )
        for num_schedulers in SCHEDULER_COUNTS
        for num_backup, weight in SYSTEMS
        for gap in GAP_CYCLES
    ]
    if len(rows) != 36 or len({case.case_id for case in rows}) != 36:
        raise AssertionError("Figure 3 must contain 36 unique cases")
    return rows
