"""Configuration and immutable constants for the isolated fill simulator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

SCHEMA_VERSION = "simulation-new/raw-v2"
LEGACY_SCHEMA_VERSION = "simulation-new/raw-v1"
TRIAL_SEEDS = (20260911, 20260912, 20260913)
DEFAULT_DECAY_FACTOR = 0.95
DEFAULT_DECAY_INTERVAL_SECONDS = 60.0
OCCUPANCY_BINS = (0.0, 0.2, 0.4, 0.6, 0.8, 0.9, 0.95, 1.0000001)


@dataclass(frozen=True)
class ModelConstants:
    """Constants that define the model rather than an experiment case.

    The official runner always uses ``OFFICIAL_CONSTANTS``. Tests may pass a
    smaller instance directly to the core so smoke checks do not alter the
    official experiment schema or expose model constants as CLI parameters.
    """

    num_nodes: int = 20_000
    num_schedulers: int = 10
    cycle_seconds: float = 0.1
    scheduler_capacity_per_cycle: int = 40
    arrivals_per_cycle: int = 100
    injection_cycles: int = 3_000
    max_cycles: int = 3_000
    node_capacity: int = 1
    score_seed: int = 0
    score_sigma: float = 0.5

    @property
    def total_pods(self) -> int:
        """Return the number of placements that fill the whole cluster.

        Injection is now continuous at a fixed rate and the run stops when every
        node is occupied, so the meaningful completion target is one placement
        per node rather than a fixed number of injected pods.
        """

        return self.num_nodes

    @property
    def max_injected_pods(self) -> int:
        """Return the upper bound on pods that could be injected before the cap."""

        return self.arrivals_per_cycle * self.injection_cycles

    @property
    def total_scheduler_capacity(self) -> int:
        """Return the maximum number of scheduling attempts per cycle."""

        return self.num_schedulers * self.scheduler_capacity_per_cycle

    def validate(self) -> None:
        """Raise ``ValueError`` when internal model constants are inconsistent."""

        integer_fields = (
            "num_nodes",
            "num_schedulers",
            "scheduler_capacity_per_cycle",
            "arrivals_per_cycle",
            "injection_cycles",
            "max_cycles",
            "node_capacity",
            "score_seed",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if self.num_nodes <= 0 or self.num_schedulers <= 0:
            raise ValueError("num_nodes and num_schedulers must be positive")
        if self.scheduler_capacity_per_cycle <= 0 or self.arrivals_per_cycle <= 0:
            raise ValueError("capacities and arrivals must be positive")
        if self.injection_cycles <= 0 or self.max_cycles < self.injection_cycles:
            raise ValueError("max_cycles must cover all injection cycles")
        if self.node_capacity != 1:
            raise ValueError("the fill model fixes node_capacity at one")
        if self.max_injected_pods < self.num_nodes:
            raise ValueError("continuous injection must be able to fill every node")
        if not math.isfinite(self.cycle_seconds) or self.cycle_seconds != 0.1:
            raise ValueError("the fill model fixes cycle_seconds at 0.1")
        if self.score_seed != 0 or self.score_sigma != 0.5:
            raise ValueError("the fill model fixes score_seed=0 and score_sigma=0.5")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation including derived constants."""

        payload = asdict(self)
        payload["total_pods"] = self.total_pods
        payload["total_scheduler_capacity"] = self.total_scheduler_capacity
        return payload


OFFICIAL_CONSTANTS = ModelConstants()


@dataclass(frozen=True)
class SimulationConfig:
    """The complete and only configurable case-level parameter set."""

    num_tiers: int
    sync_mode: str
    sync_gap_cycles: int | None
    num_backup: int
    list_mode: str
    score_threshold: float
    penalty_weight: float
    penalty_scope: str
    penalty_noise: float
    publish_gap_cycles: int
    trial_seed: int
    decay_factor: float = DEFAULT_DECAY_FACTOR
    decay_interval: float = DEFAULT_DECAY_INTERVAL_SECONDS

    @property
    def decay_enabled(self) -> bool:
        """Return whether multiplicative count decay is active."""

        return self.decay_interval > 0.0 and self.decay_factor != 1.0

    def decay_interval_cycles(
        self, constants: ModelConstants = OFFICIAL_CONSTANTS
    ) -> int | None:
        """Convert the decay interval in seconds to simulation cycles.

        A ticker cannot fire before its requested wall-clock interval, so a
        non-integral interval is rounded up to the next cycle. Disabled decay
        returns ``None``.
        """

        if not self.decay_enabled:
            return None
        ratio = self.decay_interval / constants.cycle_seconds
        return max(1, int(math.ceil(ratio - 1e-12)))

    def validate(self, constants: ModelConstants = OFFICIAL_CONSTANTS) -> None:
        """Validate a case strictly; unsupported values never fall back silently."""

        constants.validate()
        integer_fields = ("num_tiers", "num_backup", "publish_gap_cycles", "trial_seed")
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if self.num_tiers <= 0 or self.num_tiers > constants.num_nodes:
            raise ValueError("num_tiers must be in [1, num_nodes]")
        if constants.num_nodes % self.num_tiers != 0:
            raise ValueError("num_tiers must divide num_nodes into equal populations")
        if self.sync_mode not in {"event", "periodic"}:
            raise ValueError("sync_mode must be 'event' or 'periodic'")
        if self.sync_mode == "event":
            if self.sync_gap_cycles is not None:
                raise ValueError("event mode requires sync_gap_cycles=None")
        else:
            if (
                isinstance(self.sync_gap_cycles, bool)
                or not isinstance(self.sync_gap_cycles, int)
                or self.sync_gap_cycles <= 0
            ):
                raise ValueError("periodic mode requires a positive integer sync_gap_cycles")
        if self.num_backup < 0 or self.num_backup >= constants.num_nodes:
            raise ValueError("num_backup must be in [0, num_nodes-1]")
        if self.list_mode not in {"fixed", "threshold"}:
            raise ValueError("list_mode must be 'fixed' or 'threshold'")
        for name in ("score_threshold", "penalty_weight"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.list_mode == "fixed" and float(self.score_threshold) != 0.0:
            raise ValueError("fixed lists require score_threshold=0")
        if self.penalty_scope not in {"shared", "local", "noisy"}:
            raise ValueError("penalty_scope must be shared, local, or noisy")
        if isinstance(self.penalty_noise, bool) or not isinstance(self.penalty_noise, (int, float)):
            raise ValueError("penalty_noise must be numeric")
        if not math.isfinite(float(self.penalty_noise)) or float(self.penalty_noise) < 0.0:
            raise ValueError("penalty_noise must be finite and non-negative")
        if self.penalty_scope != "noisy" and float(self.penalty_noise) != 0.0:
            raise ValueError("penalty_noise must be zero unless penalty_scope='noisy'")
        if self.publish_gap_cycles <= 0:
            raise ValueError("publish_gap_cycles must be positive")
        if self.trial_seed < 0:
            raise ValueError("trial_seed must be non-negative")
        for name in ("decay_factor", "decay_interval"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if not 0.0 < float(self.decay_factor) <= 1.0:
            raise ValueError("decay_factor must be in (0, 1]")

        # Without releases or predictive local updates, an exhausted visible top
        # tier cannot be skipped until the next availability sync. Requiring at
        # least one sync opportunity per tier (plus the final drain interval)
        # rejects structurally unreachable long-gap cases before a formal run.
        availability_gap = 1 if self.sync_mode == "event" else int(self.sync_gap_cycles)
        if (self.num_tiers + 1) * max(1, availability_gap) > constants.max_cycles:
            raise ValueError(
                "num_tiers and sync_gap_cycles cannot drain within max_cycles "
                "under top-ranked fill semantics"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return all configurable fields as a JSON-compatible dictionary."""

        return asdict(self)

    def case_dict(self) -> dict[str, Any]:
        """Return the canonical case fields, excluding the trial seed."""

        payload = self.to_dict()
        payload.pop("trial_seed")
        return payload
