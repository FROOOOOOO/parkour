"""Core state machine for the isolated occupancy-conditioned fill simulator.

Each 0.1 s cycle is divided into sub-steps. In one sub-step every scheduler
issues at most ``attempts_per_sync`` decisions from its own availability view,
the binder commits that sub-step's submissions immediately, and an event-driven
scheduler refreshes from the resulting global state before its next decision.
A periodic scheduler refreshes only on its own ``G`` boundary, so the two
paradigms differ in synchronization cadence alone.

The default ``attempts_per_sync=1`` refreshes an event-driven view before every
decision. That is the setting consistent with the model boundary declared in the
paper: network transfer, snapshot installation and plugin execution time are not
represented, so event-driven propagation carries no cost and cannot lag. The
cycle then only paces pod arrivals, periodic synchronization and penalty
publication.

``attempts_per_sync=None`` collapses each cycle to a single sub-step, which is
the whole-cycle engine this module used previously: every scheduler issues all
of a cycle's decisions from one frozen view before the binder commits any of
them. It is retained so the event-driven staleness window can be swept as a
sensitivity parameter, and so that the sub-step restructuring can be shown to
have changed the synchronization window and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from config import ModelConstants, OCCUPANCY_BINS, OFFICIAL_CONSTANTS, SimulationConfig


@dataclass(frozen=True)
class CandidatePlan:
    """Reusable top-ranked selection plan for one immutable scheduler snapshot."""

    nodes_above_cutoff: np.ndarray
    scores_above_cutoff: np.ndarray
    nodes_at_cutoff: np.ndarray
    cutoff_score: float
    limit: int


@dataclass(frozen=True)
class Submission:
    """One pod submitted to the binder."""

    scheduler_id: int
    candidates: np.ndarray


def build_tiered_scores(num_nodes: int, num_tiers: int) -> tuple[np.ndarray, np.ndarray]:
    """Build fixed-seed log-normal scores and equal-population score tiers.

    Nodes retain their original identifiers. The latent scores only determine a stable
    rank; each node receives the mean latent score of its rank tier.

    :param num_nodes: number of one-capacity nodes
    :param num_tiers: number of equal-population tiers
    :return: ``(raw_scores, tier_ids)`` in original node order
    """

    if isinstance(num_nodes, bool) or not isinstance(num_nodes, int) or num_nodes <= 0:
        raise ValueError("num_nodes must be a positive integer")
    if isinstance(num_tiers, bool) or not isinstance(num_tiers, int) or num_tiers <= 0:
        raise ValueError("num_tiers must be a positive integer")
    if num_tiers > num_nodes or num_nodes % num_tiers != 0:
        raise ValueError("num_tiers must divide num_nodes into equal populations")

    latent = np.random.RandomState(0).lognormal(mean=0.0, sigma=0.5, size=num_nodes)
    rank = np.empty(num_nodes, dtype=np.int64)
    rank[np.argsort(latent, kind="stable")] = np.arange(num_nodes, dtype=np.int64)
    tier_ids = (rank * num_tiers) // num_nodes
    counts = np.bincount(tier_ids, minlength=num_tiers)
    sums = np.bincount(tier_ids, weights=latent, minlength=num_tiers)
    tier_means = sums / counts
    return tier_means[tier_ids], tier_ids


def _derive_rng(trial_seed: int, domain: int, index: int = 0) -> np.random.Generator:
    """Derive an independent deterministic PCG64 stream from a trial seed."""

    return np.random.default_rng(np.random.SeedSequence([trial_seed, domain, index]))


def _ratio(failures: np.ndarray, successes: np.ndarray) -> np.ndarray:
    """Return cumulative failure ratios, using zero for unobserved nodes."""

    denominator = failures + successes
    return np.divide(
        failures,
        denominator,
        out=np.zeros_like(failures, dtype=np.float64),
        where=denominator > 0,
    )


def decay_penalty_counts(factor: float, *count_arrays: np.ndarray) -> None:
    """Scale cumulative penalty counts in place, matching Go ``Decay()``.

    Success and failure counts are multiplied by the same floating-point factor.
    This is not a sliding window and performs no integer truncation.

    :param factor: multiplicative factor in ``(0, 1]``
    :param count_arrays: global/local success and failure count arrays
    :raises ValueError: if the factor or an array dtype cannot represent scaling
    """

    if not np.isfinite(factor) or not 0.0 < factor <= 1.0:
        raise ValueError("decay factor must be finite and in (0, 1]")
    for counts in count_arrays:
        if not np.issubdtype(counts.dtype, np.floating):
            raise ValueError("penalty decay requires floating-point count arrays")
        counts *= factor


def _optional_ratio(numerator: int | float, denominator: int | float) -> float | None:
    """Return a JSON-safe ratio, or ``None`` for zero support."""

    return float(numerator) / float(denominator) if denominator else None


def _optional_mean(total: int | float, support: int) -> float | None:
    """Return a JSON-safe mean, or ``None`` for zero support."""

    return float(total) / float(support) if support else None


class CandidateSelector:
    """Top-ranked selector with fresh per-pod random ordering inside exact ties."""

    def __init__(self, raw_scores: np.ndarray, config: SimulationConfig, num_schedulers: int):
        """Create scheduler-specific random streams for one trial."""

        if raw_scores.ndim != 1 or raw_scores.size == 0:
            raise ValueError("raw_scores must be a non-empty one-dimensional array")
        if num_schedulers <= 0:
            raise ValueError("num_schedulers must be positive")
        self.raw_scores = raw_scores
        self.normalized_scores = raw_scores / float(raw_scores.max())
        self.config = config
        self.rngs = [
            _derive_rng(config.trial_seed, 0x53434844, scheduler_id)
            for scheduler_id in range(num_schedulers)
        ]

    def prepare(
        self,
        scheduler_id: int,
        local_view: np.ndarray,
        published_global_rate: np.ndarray,
        published_local_rate: np.ndarray,
    ) -> CandidatePlan | None:
        """Prepare a reusable plan when the score vector is fixed for this cycle.

        Noisy scope deliberately cannot be prepared because its perturbation must be
        independently redrawn for every node at every pod selection lookup.
        """

        if self.config.penalty_scope == "noisy" and self.config.penalty_weight > 0:
            raise ValueError("noisy selection must use select(), not a reusable plan")
        nodes, scores = self._eligible_scores(
            scheduler_id, local_view, published_global_rate, published_local_rate, noisy=False
        )
        return self._make_plan(nodes, scores)

    def select(
        self,
        scheduler_id: int,
        local_view: np.ndarray,
        published_global_rate: np.ndarray,
        published_local_rate: np.ndarray,
    ) -> np.ndarray:
        """Select one unique, bounded candidate list from a pre-bind snapshot."""

        noisy = self.config.penalty_scope == "noisy" and self.config.penalty_weight > 0
        nodes, scores = self._eligible_scores(
            scheduler_id, local_view, published_global_rate, published_local_rate, noisy=noisy
        )
        return self.draw(self._make_plan(nodes, scores), scheduler_id)

    def draw(self, plan: CandidatePlan | None, scheduler_id: int) -> np.ndarray:
        """Draw one candidate list from a fixed top-ranked plan."""

        if plan is None:
            return np.empty(0, dtype=np.int64)
        rng = self.rngs[scheduler_id]
        required_from_cutoff = plan.limit - plan.nodes_above_cutoff.size
        if required_from_cutoff == plan.nodes_at_cutoff.size:
            cutoff_nodes = plan.nodes_at_cutoff.copy()
        else:
            cutoff_nodes = rng.choice(
                plan.nodes_at_cutoff, size=required_from_cutoff, replace=False
            )
        chosen_nodes = np.concatenate((plan.nodes_above_cutoff, cutoff_nodes))
        chosen_scores = np.concatenate(
            (
                plan.scores_above_cutoff,
                np.full(cutoff_nodes.size, plan.cutoff_score, dtype=np.float64),
            )
        )
        # A fresh key for every selected node produces an independent random order
        # inside each exact score tie while preserving strict score ordering.
        tie_keys = rng.random(chosen_nodes.size)
        order = np.lexsort((tie_keys, -chosen_scores))
        result = chosen_nodes[order].astype(np.int64, copy=False)
        if result.size != np.unique(result).size:
            raise RuntimeError("candidate selector produced duplicate node ids")
        return result

    def _eligible_scores(
        self,
        scheduler_id: int,
        local_view: np.ndarray,
        published_global_rate: np.ndarray,
        published_local_rate: np.ndarray,
        *,
        noisy: bool,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return feasible nodes and their combined scores for one lookup."""

        nodes = np.flatnonzero(local_view)
        if nodes.size == 0:
            return nodes, np.empty(0, dtype=np.float64)
        raw = self.raw_scores[nodes]
        if self.config.list_mode == "threshold":
            best_raw = float(raw.max())
            admitted = raw >= (1.0 - self.config.score_threshold) * best_raw
            nodes = nodes[admitted]
            raw = raw[admitted]
        if nodes.size == 0:
            return nodes, np.empty(0, dtype=np.float64)

        if self.config.penalty_weight == 0:
            return nodes, self.normalized_scores[nodes]
        if self.config.penalty_scope == "local":
            rates = published_local_rate[scheduler_id, nodes]
        else:
            rates = published_global_rate[nodes]
        if noisy:
            rates = np.clip(
                rates + self.rngs[scheduler_id].normal(0.0, self.config.penalty_noise, nodes.size),
                0.0,
                1.0,
            )
        weight = self.config.penalty_weight
        scores = (1.0 - weight) * self.normalized_scores[nodes] + weight * (1.0 - rates)
        return nodes, scores

    def _make_plan(self, nodes: np.ndarray, scores: np.ndarray) -> CandidatePlan | None:
        """Reduce a score vector to the strict-above and cutoff tie groups."""

        if nodes.size == 0:
            return None
        if self.config.list_mode == "threshold":
            # Threshold lists admit every node that already passed the raw-score
            # gate in ``_eligible_scores``; K does not cap the list length. The
            # cutoff is the minimum eligible score so all nodes are retained.
            limit = nodes.size
        else:
            limit = min(self.config.num_backup + 1, nodes.size)
        cutoff = float(np.partition(scores, -limit)[-limit])
        above_mask = scores > cutoff
        at_mask = scores == cutoff
        return CandidatePlan(
            nodes_above_cutoff=nodes[above_mask],
            scores_above_cutoff=scores[above_mask],
            nodes_at_cutoff=nodes[at_mask],
            cutoff_score=cutoff,
            limit=limit,
        )


class OccupancyAccumulator:
    """Raw counters and supported means for one occupancy interval."""

    def __init__(self, lo: float, hi: float):
        self.lo = lo
        self.hi = hi
        self.cycles = 0
        self.attempts = 0
        self.successes = 0
        self.local_failures = 0
        self.binder_failures = 0
        self.submitted_pods = 0
        self.candidate_slots = 0
        self.candidate_checks = 0
        self.candidate_rejections = 0
        self.support = 0
        self.occupancy_sum = 0.0
        self.idle_nodes_sum = 0.0
        self.primary_demand_sum = 0.0
        self.t_eff_sum = 0.0
        self.visible_top_tier_sum = 0.0
        self.visible_top_tier_support = 0
        self.global_top_tier_sum = 0.0
        self.global_top_tier_support = 0

    def add(self, metrics: dict[str, Any]) -> None:
        """Charge one complete cycle to this occupancy interval."""

        self.cycles += 1
        for name in (
            "attempts",
            "successes",
            "local_failures",
            "binder_failures",
            "submitted_pods",
            "candidate_slots",
            "candidate_checks",
            "candidate_rejections",
        ):
            setattr(self, name, getattr(self, name) + int(metrics[name]))
        self.occupancy_sum += float(metrics["occupancy"])
        self.idle_nodes_sum += float(metrics["idle_nodes"])
        if metrics["primary_demand"] is not None:
            self.support += 1
            self.primary_demand_sum += float(metrics["primary_demand"])
            self.t_eff_sum += float(metrics["t_eff"])
        if metrics["visible_top_tier"] is not None:
            self.visible_top_tier_support += 1
            self.visible_top_tier_sum += float(metrics["visible_top_tier"])
        if metrics["global_top_tier"] is not None:
            self.global_top_tier_support += 1
            self.global_top_tier_sum += float(metrics["global_top_tier"])

    def to_dict(self) -> dict[str, Any]:
        """Return raw counters, supports, and derived values for JSON output."""

        return {
            "lo": self.lo,
            "hi": self.hi,
            "cycles": self.cycles,
            "attempts": self.attempts,
            "successes": self.successes,
            "local_failures": self.local_failures,
            "binder_failures": self.binder_failures,
            "total_conflict_rate": _optional_ratio(
                self.local_failures + self.binder_failures, self.attempts
            ),
            "submitted_pods": self.submitted_pods,
            "candidate_slots": self.candidate_slots,
            "avg_candidates": _optional_ratio(self.candidate_slots, self.submitted_pods),
            "candidate_checks": self.candidate_checks,
            "candidate_rejections": self.candidate_rejections,
            "candidate_rejection_ratio": _optional_ratio(
                self.candidate_rejections, self.candidate_checks
            ),
            "support": self.support,
            "occupancy_sum": self.occupancy_sum,
            "idle_nodes_sum": self.idle_nodes_sum,
            "occupancy_mean": _optional_mean(self.occupancy_sum, self.cycles),
            "idle_nodes_mean": _optional_mean(self.idle_nodes_sum, self.cycles),
            "primary_demand_sum": self.primary_demand_sum,
            "primary_demand_mean": _optional_mean(self.primary_demand_sum, self.support),
            "t_eff_sum": self.t_eff_sum,
            "t_eff_mean": _optional_mean(self.t_eff_sum, self.support),
            "visible_top_tier_sum": self.visible_top_tier_sum,
            "visible_top_tier_support": self.visible_top_tier_support,
            "visible_top_tier_mean": _optional_mean(
                self.visible_top_tier_sum, self.visible_top_tier_support
            ),
            "global_top_tier_sum": self.global_top_tier_sum,
            "global_top_tier_support": self.global_top_tier_support,
            "global_top_tier_mean": _optional_mean(
                self.global_top_tier_sum, self.global_top_tier_support
            ),
        }


def _top_tier_width(availability: np.ndarray, raw_scores: np.ndarray) -> float | None:
    """Return the remaining width of the highest raw-score available tier."""

    nodes = np.flatnonzero(availability)
    if nodes.size == 0:
        return None
    visible_scores = raw_scores[nodes]
    return float(np.count_nonzero(visible_scores == visible_scores.max()))


def _occupancy_bin_index(occupancy: float) -> int:
    """Map a cycle-start occupancy to the frozen left-closed intervals."""

    index = int(np.searchsorted(OCCUPANCY_BINS, occupancy, side="right") - 1)
    return min(max(index, 0), len(OCCUPANCY_BINS) - 2)


def _empty_cycle_metrics() -> dict[str, list[Any]]:
    """Allocate the compact dict-of-arrays cycle trace."""

    names = (
        "cycle_index",
        "occupancy",
        "idle_nodes",
        "attempts",
        "successes",
        "local_failures",
        "binder_failures",
        "submitted_pods",
        "candidate_checks",
        "candidate_rejections",
        "primary_demand",
        "t_eff",
        "visible_top_tier",
        "global_top_tier",
        "local_global_mismatches",
        "penalty_decay_applied",
    )
    return {name: [] for name in names}


def simulate(
    config: SimulationConfig,
    *,
    constants: ModelConstants = OFFICIAL_CONSTANTS,
    include_cycle_metrics: bool = False,
    attempts_per_sync: int | None = 1,
) -> dict[str, Any]:
    """Run one deterministic fill trial and return its raw run payload.

    Within a cycle the order is arrivals, availability synchronization, an
    optional penalty-count decay tick, penalty publication, uniform dispatch,
    and then one or more sub-steps. Each sub-step builds every scheduler's
    candidate lists from its own pre-bind view, permutes the resulting
    submissions with the seeded binder stream, commits them serially, and -
    under event-driven synchronization - refreshes the views before the next
    sub-step. Failed pods requeue at the end of the cycle.

    Args:
        config: Immutable case configuration; ``trial_seed`` selects the run.
        constants: Immutable model constants (node count, cycle length, rates).
        include_cycle_metrics: Record the full per-cycle trace when true.
        attempts_per_sync: Decisions each scheduler issues between view
            refreshes inside one cycle. ``1``, the default and the production
            setting, refreshes an event-driven view before every decision.
            ``None`` uses one sub-step per cycle, reproducing the whole-cycle
            engine described in the module docstring. Values
            in between sweep the staleness window. Periodic schedulers ignore
            the refresh but still bind per sub-step, so both paradigms share
            one binder model.

    Returns:
        ``summary``, ``occupancy`` and, when requested, ``cycle_metrics``.
        ``summary`` also carries ``attempts_per_sync`` and ``substeps`` so a
        cache records the window it was produced under.

    Raises:
        ValueError: ``attempts_per_sync`` is not ``None`` or a positive integer.
        RuntimeError: The fill did not occupy every node within ``max_cycles``,
            or an attempt/candidate accounting invariant broke.
    """

    if attempts_per_sync is not None and attempts_per_sync < 1:
        raise ValueError("attempts_per_sync must be None or a positive integer")

    config.validate(constants)
    raw_scores, _ = build_tiered_scores(constants.num_nodes, config.num_tiers)
    selector = CandidateSelector(raw_scores, config, constants.num_schedulers)
    binder_rng = _derive_rng(config.trial_seed, 0x42494E44)
    event_driven = config.sync_mode == "event"

    global_available = np.ones(constants.num_nodes, dtype=bool)
    # Per-scheduler availability views, identical in role to ``core.simulate``.
    # The sub-step loop refreshes them more often under event-driven
    # synchronization; a refresh also clears the optimistic primary reservations
    # of the previous sub-step, which is correct because those pods have by then
    # either committed (the node is globally taken) or been requeued.
    local_available = np.broadcast_to(
        global_available, (constants.num_schedulers, constants.num_nodes)
    ).copy()
    global_failures = np.zeros(constants.num_nodes, dtype=np.float64)
    global_successes = np.zeros(constants.num_nodes, dtype=np.float64)
    local_failures_by_node = np.zeros(
        (constants.num_schedulers, constants.num_nodes), dtype=np.float64
    )
    local_successes_by_node = np.zeros_like(local_failures_by_node)
    shadow_global_failures = np.zeros_like(global_failures)
    shadow_global_successes = np.zeros_like(global_successes)
    shadow_local_failures = np.zeros_like(local_failures_by_node)
    shadow_local_successes = np.zeros_like(local_successes_by_node)
    published_global_rate = np.zeros(constants.num_nodes, dtype=np.float64)
    published_local_rate = np.zeros_like(local_failures_by_node, dtype=np.float64)
    penalty_first_observation_cycle = np.full(constants.num_nodes, -1, dtype=np.int64)
    penalty_last_observation_cycle = np.full(constants.num_nodes, -1, dtype=np.int64)

    bins = [
        OccupancyAccumulator(OCCUPANCY_BINS[i], OCCUPANCY_BINS[i + 1])
        for i in range(len(OCCUPANCY_BINS) - 1)
    ]
    cycle_trace = _empty_cycle_metrics() if include_cycle_metrics else None

    queue_size = 0
    dispatch_cursor = 0
    total_attempts = 0
    total_successes = 0
    total_local_failures = 0
    total_binder_failures = 0
    total_submitted = 0
    total_candidate_slots = 0
    total_candidate_checks = 0
    total_candidate_rejections = 0
    binder_arbitration_max_per_pod = 0
    total_score = 0.0
    primary_demand_sum = 0.0
    t_eff_sum = 0.0
    diagnostic_support = 0
    elapsed_cycles = 0
    total_substeps = 0
    decay_interval_cycles = config.decay_interval_cycles(constants)
    penalty_decay_ticks = 0
    penalty_publish_events = 0
    penalty_signal_publish_events_changed = 0
    penalty_signal_differing_values = 0
    penalty_signal_max_abs_delta = 0.0

    for cycle in range(constants.max_cycles):
        if total_successes >= constants.num_nodes:
            break

        if cycle < constants.injection_cycles:
            queue_size += constants.arrivals_per_cycle

        if event_driven or cycle % int(config.sync_gap_cycles or 1) == 0:
            local_available[:] = global_available
        decay_applied = bool(
            decay_interval_cycles is not None
            and cycle > 0
            and cycle % decay_interval_cycles == 0
        )
        if decay_applied:
            decay_penalty_counts(
                config.decay_factor,
                global_failures,
                global_successes,
                local_failures_by_node,
                local_successes_by_node,
            )
            penalty_decay_ticks += 1
        if cycle % config.publish_gap_cycles == 0:
            published_global_rate = _ratio(global_failures, global_successes)
            published_local_rate = _ratio(local_failures_by_node, local_successes_by_node)
            shadow_global_rate = _ratio(shadow_global_failures, shadow_global_successes)
            shadow_local_rate = _ratio(shadow_local_failures, shadow_local_successes)
            if config.penalty_scope == "local":
                signal_delta = np.abs(published_local_rate - shadow_local_rate)
            else:
                signal_delta = np.abs(published_global_rate - shadow_global_rate)
            changed_values = int(np.count_nonzero(signal_delta > 1e-12))
            penalty_publish_events += 1
            penalty_signal_differing_values += changed_values
            penalty_signal_publish_events_changed += int(changed_values > 0)
            if signal_delta.size:
                penalty_signal_max_abs_delta = max(
                    penalty_signal_max_abs_delta, float(signal_delta.max())
                )

        # Occupancy diagnostics are charged at cycle granularity exactly as in
        # ``core.simulate``, so occupancy bins stay comparable across engines.
        idle_nodes = int(np.count_nonzero(global_available))
        occupancy = float(constants.num_nodes - idle_nodes) / float(constants.num_nodes)
        visible_top = _top_tier_width(local_available[0], raw_scores)
        global_top = _top_tier_width(global_available, raw_scores)
        local_global_mismatches = int(
            np.count_nonzero(local_available[0] != global_available)
        )

        attempt_count = min(queue_size, constants.total_scheduler_capacity)
        dispatch_counts = np.zeros(constants.num_schedulers, dtype=np.int64)
        for offset in range(attempt_count):
            scheduler_id = (dispatch_cursor + offset) % constants.num_schedulers
            dispatch_counts[scheduler_id] += 1
        if dispatch_counts.max(initial=0) > constants.scheduler_capacity_per_cycle:
            raise RuntimeError("uniform dispatcher exceeded per-scheduler capacity")
        dispatch_cursor = (dispatch_cursor + attempt_count) % constants.num_schedulers
        queue_size -= attempt_count

        cycle_local_failures = 0
        cycle_candidate_slots = 0
        cycle_successes = 0
        cycle_binder_failures = 0
        cycle_candidate_checks = 0
        cycle_candidate_rejections = 0
        cycle_binder_arbitration_max = 0
        cycle_submitted = 0
        primary_picks: list[int] = []

        # One sub-step issues at most ``substep_size`` decisions per scheduler and
        # then binds them. ``None`` makes the whole cycle a single sub-step, which
        # restores the two-phase order of ``core.simulate``.
        remaining = dispatch_counts.copy()
        substep_size = (
            attempts_per_sync
            if attempts_per_sync is not None
            else max(1, int(remaining.max(initial=0)))
        )
        first_substep = True
        while remaining.sum() > 0:
            if not first_substep and event_driven:
                # Refresh before the next round of decisions: this is the change
                # the whole module exists for. Periodic schedulers deliberately
                # keep their frozen view until their own G boundary.
                local_available[:] = global_available
            first_substep = False
            total_substeps += 1

            submissions: list[Submission] = []
            for scheduler_id in range(constants.num_schedulers):
                take = int(min(remaining[scheduler_id], substep_size))
                if take == 0:
                    continue
                view = local_available[scheduler_id]
                for _ in range(take):
                    candidates = selector.select(
                        scheduler_id,
                        view,
                        published_global_rate,
                        published_local_rate,
                    )
                    if candidates.size == 0:
                        cycle_local_failures += 1
                        continue
                    submissions.append(Submission(scheduler_id, candidates))
                    cycle_candidate_slots += int(candidates.size)
                    primary = int(candidates[0])
                    primary_picks.append(primary)
                    # Optimistic local reservation of the primary only, so no
                    # ghost occupancy accrues on backups. Under per-decision
                    # refresh it is redundant but harmless; it still guards the
                    # periodic path and any ``attempts_per_sync > 1``.
                    view[primary] = False
                remaining[scheduler_id] -= take

            if submissions:
                cycle_submitted += len(submissions)
                binder_order = binder_rng.permutation(len(submissions))
                for submission_index in binder_order:
                    submission = submissions[int(submission_index)]
                    accepted = False
                    pod_arbitration_checks = 0
                    for node in submission.candidates:
                        cycle_candidate_checks += 1
                        pod_arbitration_checks += 1
                        if penalty_first_observation_cycle[node] < 0:
                            penalty_first_observation_cycle[node] = cycle
                        penalty_last_observation_cycle[node] = cycle
                        if global_available[node]:
                            global_available[node] = False
                            global_successes[node] += 1
                            local_successes_by_node[submission.scheduler_id, node] += 1
                            shadow_global_successes[node] += 1
                            shadow_local_successes[submission.scheduler_id, node] += 1
                            cycle_successes += 1
                            total_score += float(raw_scores[node])
                            accepted = True
                            break
                        global_failures[node] += 1
                        local_failures_by_node[submission.scheduler_id, node] += 1
                        shadow_global_failures[node] += 1
                        shadow_local_failures[submission.scheduler_id, node] += 1
                        cycle_candidate_rejections += 1
                    if pod_arbitration_checks > cycle_binder_arbitration_max:
                        cycle_binder_arbitration_max = pod_arbitration_checks
                    if not accepted:
                        cycle_binder_failures += 1

        queue_size += cycle_local_failures + cycle_binder_failures
        if primary_picks:
            pick_counts = np.unique(
                np.asarray(primary_picks, dtype=np.int64), return_counts=True
            )[1]
            primary_demand = float(pick_counts.sum())
            t_eff = primary_demand * primary_demand / float(
                np.square(pick_counts, dtype=np.float64).sum()
            )
            diagnostic_support += 1
            primary_demand_sum += primary_demand
            t_eff_sum += t_eff
        else:
            primary_demand = None
            t_eff = None

        cycle_data = {
            "cycle_index": cycle,
            "occupancy": occupancy,
            "idle_nodes": idle_nodes,
            "attempts": attempt_count,
            "successes": cycle_successes,
            "local_failures": cycle_local_failures,
            "binder_failures": cycle_binder_failures,
            "submitted_pods": cycle_submitted,
            "candidate_slots": cycle_candidate_slots,
            "candidate_checks": cycle_candidate_checks,
            "candidate_rejections": cycle_candidate_rejections,
            "primary_demand": primary_demand,
            "t_eff": t_eff,
            "visible_top_tier": visible_top,
            "global_top_tier": global_top,
            "local_global_mismatches": local_global_mismatches,
            "penalty_decay_applied": decay_applied,
        }
        bins[_occupancy_bin_index(occupancy)].add(cycle_data)
        if cycle_trace is not None:
            for name in cycle_trace:
                cycle_trace[name].append(cycle_data[name])

        total_attempts += attempt_count
        total_successes += cycle_successes
        total_local_failures += cycle_local_failures
        total_binder_failures += cycle_binder_failures
        total_submitted += cycle_submitted
        total_candidate_slots += cycle_candidate_slots
        total_candidate_checks += cycle_candidate_checks
        total_candidate_rejections += cycle_candidate_rejections
        if cycle_binder_arbitration_max > binder_arbitration_max_per_pod:
            binder_arbitration_max_per_pod = cycle_binder_arbitration_max
        elapsed_cycles += 1

    if total_successes != constants.num_nodes:
        raise RuntimeError(
            "fill did not occupy every node within max_cycles: "
            f"successes={total_successes}, queue={queue_size}, elapsed={elapsed_cycles}"
        )
    if total_attempts != total_successes + total_local_failures + total_binder_failures:
        raise RuntimeError("attempt accounting invariant failed")
    if total_candidate_checks != total_successes + total_candidate_rejections:
        raise RuntimeError("candidate check accounting invariant failed")

    observed_nodes = penalty_first_observation_cycle >= 0
    observation_spans = (
        penalty_last_observation_cycle[observed_nodes]
        - penalty_first_observation_cycle[observed_nodes]
    ).astype(np.float64)
    if observation_spans.size:
        span_p50, span_p90, span_p99 = np.percentile(observation_spans, [50, 90, 99])
        span_mean = float(observation_spans.mean())
        span_max = float(observation_spans.max())
    else:
        span_p50 = span_p90 = span_p99 = span_mean = span_max = None

    summary = {
        "elapsed_cycles": elapsed_cycles,
        "attempts": total_attempts,
        "successes": total_successes,
        "local_failures": total_local_failures,
        "binder_failures": total_binder_failures,
        "total_conflict_rate": _optional_ratio(
            total_local_failures + total_binder_failures, total_attempts
        ),
        "candidate_checks": total_candidate_checks,
        "candidate_rejections": total_candidate_rejections,
        "candidate_rejection_ratio": _optional_ratio(
            total_candidate_rejections, total_candidate_checks
        ),
        "submitted_pods": total_submitted,
        "candidate_slots": total_candidate_slots,
        "avg_candidates": _optional_ratio(total_candidate_slots, total_submitted),
        "binder_arbitration_checks": total_candidate_checks,
        "binder_arbitration_mean_per_pod": _optional_ratio(
            total_candidate_checks, total_submitted
        ),
        "binder_arbitration_max_per_pod": binder_arbitration_max_per_pod,
        "throughput": constants.total_pods / (elapsed_cycles * constants.cycle_seconds),
        "score_sum": total_score,
        "avg_score": _optional_ratio(total_score, total_successes),
        "primary_demand_sum": primary_demand_sum,
        "primary_demand_support": diagnostic_support,
        "primary_demand_mean": _optional_mean(primary_demand_sum, diagnostic_support),
        "t_eff_sum": t_eff_sum,
        "t_eff_support": diagnostic_support,
        "t_eff_mean": _optional_mean(t_eff_sum, diagnostic_support),
        "penalty_decay_ticks": penalty_decay_ticks,
        "penalty_decay_interval_cycles": decay_interval_cycles,
        "penalty_success_mass_final": float(global_successes.sum()),
        "penalty_failure_mass_final": float(global_failures.sum()),
        "penalty_observed_nodes": int(observation_spans.size),
        "penalty_observation_span_mean_cycles": span_mean,
        "penalty_observation_span_p50_cycles": (
            None if span_p50 is None else float(span_p50)
        ),
        "penalty_observation_span_p90_cycles": (
            None if span_p90 is None else float(span_p90)
        ),
        "penalty_observation_span_p99_cycles": (
            None if span_p99 is None else float(span_p99)
        ),
        "penalty_observation_span_max_cycles": span_max,
        "penalty_success_updates_max_per_node": float(shadow_global_successes.max()),
        "penalty_publish_events": penalty_publish_events,
        "penalty_signal_publish_events_changed": penalty_signal_publish_events_changed,
        "penalty_signal_differing_values": penalty_signal_differing_values,
        "penalty_signal_max_abs_delta": penalty_signal_max_abs_delta,
        # Provenance for this engine only.
        "attempts_per_sync": attempts_per_sync,
        "substeps": total_substeps,
    }
    payload: dict[str, Any] = {
        "summary": summary,
        "occupancy": [bucket.to_dict() for bucket in bins],
    }
    if cycle_trace is not None:
        payload["cycle_metrics"] = cycle_trace
    return payload
