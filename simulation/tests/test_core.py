"""Unit and small-regression tests for the isolated fill simulator."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import ModelConstants, SimulationConfig  # noqa: E402
from core import (  # noqa: E402
    CandidateSelector,
    build_tiered_scores,
    decay_penalty_counts,
    simulate,
)


def small_constants() -> ModelConstants:
    """Return a fast continuous-injection fill model over 200 nodes."""

    return ModelConstants(
        num_nodes=200,
        num_schedulers=10,
        cycle_seconds=0.1,
        scheduler_capacity_per_cycle=40,
        arrivals_per_cycle=100,
        injection_cycles=100,
        max_cycles=100,
    )


def base_config(**updates: object) -> SimulationConfig:
    """Return a valid tiered-10 event-driven test configuration."""

    values: dict[str, object] = {
        "num_tiers": 10,
        "sync_mode": "event",
        "sync_gap_cycles": None,
        "num_backup": 2,
        "list_mode": "fixed",
        "score_threshold": 0.0,
        "penalty_weight": 0.0,
        "penalty_scope": "shared",
        "penalty_noise": 0.0,
        "publish_gap_cycles": 1,
        "trial_seed": 20260911,
    }
    values.update(updates)
    return SimulationConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "config",
    [
        base_config(num_tiers=0),
        base_config(num_tiers=3),
        base_config(sync_mode="unknown"),
        base_config(sync_mode="periodic", sync_gap_cycles=None),
        base_config(sync_gap_cycles=1),
        base_config(num_backup=-1),
        base_config(list_mode="other"),
        base_config(score_threshold=1.1),
        base_config(score_threshold=0.1),
        base_config(penalty_weight=-0.1),
        base_config(penalty_scope="other"),
        base_config(penalty_noise=0.1),
        base_config(publish_gap_cycles=0),
        base_config(trial_seed=-1),
        base_config(decay_factor=0.0),
        base_config(decay_factor=1.1),
        base_config(decay_interval=float("nan")),
        base_config(num_tiers=50, sync_mode="periodic", sync_gap_cycles=5),
    ],
)
def test_invalid_config_raises(config: SimulationConfig) -> None:
    """Every unsupported configuration must fail instead of silently falling back."""

    with pytest.raises(ValueError):
        config.validate(small_constants())


def test_tiered_scores_are_equal_population_and_preserve_order() -> None:
    """Tier construction returns exact equal-size groups in original node order."""

    scores, tiers = build_tiered_scores(20, 5)
    assert scores.shape == (20,)
    assert tiers.shape == (20,)
    assert np.bincount(tiers).tolist() == [4, 4, 4, 4, 4]
    assert np.all(np.diff(np.unique(scores)) > 0)
    scores_again, tiers_again = build_tiered_scores(20, 5)
    np.testing.assert_array_equal(scores, scores_again)
    np.testing.assert_array_equal(tiers, tiers_again)


def test_candidate_lists_are_unique_and_bounded() -> None:
    """Fixed lists contain at most K+1 distinct feasible nodes."""

    raw = np.array([1.0, 3.0, 3.0, 2.0, 3.0, 1.0])
    config = base_config(num_tiers=1, num_backup=2)
    selector = CandidateSelector(raw, config, num_schedulers=1)
    view = np.array([True, True, True, True, True, False])
    global_rate = np.zeros(6)
    local_rate = np.zeros((1, 6))
    for _ in range(20):
        candidates = selector.select(0, view, global_rate, local_rate)
        assert len(candidates) == 3
        assert len(set(candidates)) == 3
        assert set(candidates).issubset({0, 1, 2, 3, 4})
        assert all(raw[node] == 3.0 for node in candidates)


def test_threshold_admits_every_node_above_score_gate_without_cap() -> None:
    """Threshold lists include all feasible nodes above the gate; K is not a cap."""

    raw = np.array([10.0, 10.0, 10.0, 10.0, 9.0, 8.99, 1.0])
    config = base_config(
        num_tiers=1,
        num_backup=1,
        list_mode="threshold",
        score_threshold=0.1,
    )
    selector = CandidateSelector(raw, config, num_schedulers=1)
    candidates = selector.select(0, np.ones(7, dtype=bool), np.zeros(7), np.zeros((1, 7)))
    # best raw is 10; the gate keeps raw >= 9, i.e. nodes 0..4, and K=1 does not
    # truncate the list to two entries any more.
    assert set(candidates.tolist()) == {0, 1, 2, 3, 4}
    assert len(set(candidates.tolist())) == 5
    assert 5 not in candidates and 6 not in candidates


def test_simulation_is_reproducible() -> None:
    """The same complete config yields byte-equivalent Python result objects."""

    config = base_config(sync_mode="periodic", sync_gap_cycles=5, publish_gap_cycles=5)
    first = simulate(config, constants=small_constants(), include_cycle_metrics=True)
    second = simulate(config, constants=small_constants(), include_cycle_metrics=True)
    assert first == second


def test_fill_is_monotone_and_binds_every_pod() -> None:
    """Fill has no release, reaches all nodes, and records event snapshots exactly."""

    constants = small_constants()
    result = simulate(base_config(), constants=constants, include_cycle_metrics=True)
    trace = result["cycle_metrics"]
    occupancy = trace["occupancy"]
    assert occupancy == sorted(occupancy)
    assert result["summary"]["successes"] == constants.num_nodes
    assert trace["successes"][-1] > 0
    assert all(value == 0 for value in trace["local_global_mismatches"])


def test_same_cadence_shared_penalty_is_inert_in_fill() -> None:
    """A same-cadence shared penalty cannot rank nodes not already filtered in fill."""

    constants = small_constants()
    baseline = base_config(
        sync_mode="periodic", sync_gap_cycles=5, publish_gap_cycles=5, penalty_weight=0.0
    )
    penalty = replace(baseline, penalty_weight=0.5)
    baseline_result = simulate(baseline, constants=constants, include_cycle_metrics=True)
    penalty_result = simulate(penalty, constants=constants, include_cycle_metrics=True)
    assert baseline_result == penalty_result


def test_metric_denominators_and_occupancy_sums_are_conserved() -> None:
    """Raw counters expose and satisfy all conflict and candidate denominators."""

    result = simulate(
        base_config(sync_mode="periodic", sync_gap_cycles=5),
        constants=small_constants(),
    )
    summary = result["summary"]
    assert summary["attempts"] == (
        summary["successes"] + summary["local_failures"] + summary["binder_failures"]
    )
    assert summary["candidate_checks"] == (
        summary["successes"] + summary["candidate_rejections"]
    )
    assert sum(row["attempts"] for row in result["occupancy"]) == summary["attempts"]
    assert sum(row["successes"] for row in result["occupancy"]) == summary["successes"]
    assert sum(row["candidate_checks"] for row in result["occupancy"]) == summary["candidate_checks"]
    for row in result["occupancy"]:
        if row["cycles"] == 0:
            assert row["occupancy_mean"] is None
            assert row["idle_nodes_mean"] is None
            assert row["total_conflict_rate"] is None


def test_decay_interval_converts_seconds_to_cycles() -> None:
    """Decay uses wall-clock seconds and never fires before the requested interval."""

    config = base_config(decay_factor=0.95, decay_interval=60.0)
    assert config.decay_interval_cycles() == 600
    assert replace(config, decay_interval=0.25).decay_interval_cycles(small_constants()) == 3
    assert replace(config, decay_interval=0.0).decay_interval_cycles(small_constants()) is None
    assert replace(config, decay_factor=1.0).decay_interval_cycles(small_constants()) is None


def test_decay_scales_success_and_failure_counts_without_truncation() -> None:
    """One decay tick multiplies every cumulative counter by the same factor."""

    successes = np.array([1.0, 10.0, 3.5])
    failures = np.array([2.0, 4.0, 0.5])
    decay_penalty_counts(0.95, successes, failures)
    np.testing.assert_allclose(successes, [0.95, 9.5, 3.325])
    np.testing.assert_allclose(failures, [1.9, 3.8, 0.475])


def test_disabled_decay_reproduces_no_decay_behavior() -> None:
    """Both documented disable switches preserve the exact legacy trajectory."""

    constants = small_constants()
    factor_disabled = base_config(decay_factor=1.0, decay_interval=0.2)
    interval_disabled = base_config(decay_factor=0.95, decay_interval=0.0)
    first = simulate(factor_disabled, constants=constants, include_cycle_metrics=True)
    second = simulate(interval_disabled, constants=constants, include_cycle_metrics=True)
    assert first == second
    assert first["summary"]["penalty_decay_ticks"] == 0
    assert first["summary"]["penalty_decay_interval_cycles"] is None
    assert first["summary"]["penalty_signal_publish_events_changed"] == 0
    assert first["summary"]["penalty_signal_differing_values"] == 0
    assert first["summary"]["penalty_signal_max_abs_delta"] == 0.0


def test_active_decay_mass_matches_cycle_recurrence() -> None:
    """Cycle-start ticks scale prior counts before this cycle's binder outcomes."""

    constants = small_constants()
    result = simulate(
        base_config(decay_factor=0.5, decay_interval=0.2),
        constants=constants,
        include_cycle_metrics=True,
    )
    trace = result["cycle_metrics"]
    success_mass = 0.0
    failure_mass = 0.0
    ticks = 0
    for decayed, successes, failures in zip(
        trace["penalty_decay_applied"],
        trace["successes"],
        trace["candidate_rejections"],
    ):
        if decayed:
            success_mass *= 0.5
            failure_mass *= 0.5
            ticks += 1
        success_mass += successes
        failure_mass += failures
    summary = result["summary"]
    assert summary["penalty_decay_interval_cycles"] == 2
    assert summary["penalty_decay_ticks"] == ticks
    assert summary["penalty_success_mass_final"] == pytest.approx(success_mass)
    assert summary["penalty_failure_mass_final"] == pytest.approx(failure_mass)
    assert summary["penalty_observed_nodes"] == constants.num_nodes
    assert summary["penalty_success_updates_max_per_node"] == 1.0
    assert 0 <= summary["penalty_observation_span_p50_cycles"]
    assert summary["penalty_observation_span_p50_cycles"] <= summary["penalty_observation_span_p90_cycles"]
    assert summary["penalty_observation_span_p90_cycles"] <= summary["penalty_observation_span_p99_cycles"]
    assert summary["penalty_observation_span_p99_cycles"] <= summary["penalty_observation_span_max_cycles"]


def test_dispatch_uses_aggregate_capacity_and_keeps_remainder() -> None:
    """Dispatch is capacity-bounded without stranding a queue remainder."""

    # Three schedulers can issue 120 attempts/cycle. The first 100 arrivals are
    # all dispatched even though 100 is not divisible by three.
    constants = ModelConstants(
        num_nodes=20,
        num_schedulers=3,
        cycle_seconds=0.1,
        scheduler_capacity_per_cycle=40,
        arrivals_per_cycle=100,
        injection_cycles=100,
        max_cycles=100,
    )
    result = simulate(base_config(num_backup=0), constants=constants, include_cycle_metrics=True)
    trace = result["cycle_metrics"]
    assert trace["attempts"][0] == 100
    assert trace["attempts"][0] % constants.num_schedulers != 0
    assert all(a <= constants.total_scheduler_capacity for a in trace["attempts"])


@pytest.mark.parametrize(
    ("sync_mode", "sync_gap_cycles"),
    [("event", None), ("periodic", 5)],
)
def test_single_scheduler_primary_assume_eliminates_self_conflict(
    sync_mode: str, sync_gap_cycles: int | None
) -> None:
    """One scheduler reserves distinct primaries and never conflicts with itself."""

    constants = replace(small_constants(), num_schedulers=1)
    result = simulate(
        base_config(sync_mode=sync_mode, sync_gap_cycles=sync_gap_cycles),
        constants=constants,
    )
    summary = result["summary"]
    assert summary["binder_failures"] == 0
    assert summary["local_failures"] == 0
    assert summary["total_conflict_rate"] == 0.0

