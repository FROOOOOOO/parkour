"""Synthetic raw results, for testing the reduction without the real ones.

`reduce.py` reads raw results that are not in the repository, so its tests write
small ones. For each artifact family there is a writer that lays out, the way
the runners record them, only the files and fields the reduction reads, from a
short description of the trial; and a function that states, from the same
description and by the metric definitions, the record the archive should then
hold. The records are built over the field lists in `common/schema.py`, so a
field added there fails these tests until both the reducer and this fixture
account for it. The CL2 logs use the step names `common/cl2.py` looks for.
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from common import cl2, schema

START = datetime(2026, 4, 27, 2, 53, 4, 806007, tzinfo=timezone.utc)
NAMESPACES = ("test-fixture-1", "test-fixture-2")
#: (share of the saturation phase elapsed, share of the pods placed) at each
#: round of pod summaries; the occupancy boundaries fall on these.
FILL = ((0.25, 0.5), (0.5, 0.8), (0.75, 0.9), (1.0, 1.0))
#: Placed pods a summary reports as pending rather than running. Placement
#: counts both, and a nonzero value makes that observable.
PENDING = 3


@dataclass
class Overhead:
    """What a trial's saturation-phase latency and resource metrics report.

    Latencies are P99s in seconds, as the collector's histogram snapshots hold
    them; CPU (cores) and resident memory (bytes) are each pod's value at the
    end of the phase. None means the collector recorded no such metric.
    """

    algo_p99: float | None = 0.2521
    e2e_p99: float | None = 19.911
    scheduler_cpu: tuple[float, ...] | None = (1.5, 1.25)
    scheduler_rss: tuple[int, ...] | None = (1_600_000_000, 1_650_000_000)
    binder_cpu: float | None = 0.48
    dispatcher_cpu: float | None = 0.305


@dataclass
class Trial:
    """One trial: how long its saturation phase ran and what it counted."""

    number: int
    duration: float  # seconds, to the microsecond
    acf: int = 0  # all-candidates-failed escalations
    bind_conflicts: int = 0  # candidate-level bind failures
    not_scheduled: int = 0  # pods left unscheduled when the wait timed out
    # One-pod latency deployments that then also timed out; their pods are not
    # part of the saturation workload.
    latency_timeouts: int = 0
    start: datetime = START
    overhead: Overhead = field(default_factory=Overhead)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def seconds(self) -> float:
        """The phase length, computed the way the reduction computes it."""

        return self.end.timestamp() - self.start.timestamp()


def expected_pods(params: dict[str, Any]) -> int:
    return int(params["num_nodes"]) * int(params["pods_per_node"])


def _dump(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2)


# ---------------------------------------------------------------------------
#  The CL2 log
# ---------------------------------------------------------------------------

def klog(when: datetime, message: str, *, severity: str = "I",
         source: str = "simple_test_executor.go:162") -> str:
    return (f"{severity}{when:%m%d %H:%M:%S}.{when.microsecond:06d} "
            f"{4242:>7} {source}] {message}\n")


def _step(when: datetime, number: int, name: str, event: str) -> str:
    return klog(when, f'Step "[step: {number:02d}] {name}" {event}')


def _pods(namespace: str, controller: str, replicas: int, placed: int,
          separator: str) -> str:
    """A pod summary. CL2 separates it from the controller with ': ' while
    waiting and with ' - summary of pods : ' in a timeout error. A few placed
    pods are still pending: bound, but not yet running."""

    pending = min(PENDING, placed)
    running = placed - pending
    return (f"namespace({namespace}), controlledBy({controller}){separator}"
            f"Pods: {replicas} out of {replicas} created, {running} running "
            f"({running} updated), {pending} pending scheduled, "
            f"{replicas - placed} not scheduled, "
            "0 inactive, 0 terminating, 0 unknown, 0 runningButNotReady ")


def _timeout(when: datetime, controller: str, replicas: int, placed: int) -> str:
    """A wait deadline expiring, which CL2 logs as an error with the
    controller's last pod summary."""

    summary = _pods(NAMESPACES[0], controller, replicas, placed, " - summary of pods : ")
    return klog(when, f"WaitForControlledPodsRunning: error for {NAMESPACES[0]}/{controller}: "
                f"got context deadline exceeded while waiting for {replicas} pods to be "
                f"running in {summary}",
                severity="E", source="wait_for_controlled_pods.go:627")


def cl2_log(trial: Trial, pods: int) -> str:
    """A CL2 log whose saturation phase runs from `trial.start` to `trial.end`.

    A complete trial logs pod summaries that pass 80%, 90% and 100% placement
    at the FILL points. A trial with unscheduled pods ends in a wait timeout,
    logged as an error, as CL2 does. Latency timeouts follow in a latency phase
    after the saturation phase. Any timeout makes CL2 report failure.
    """

    replicas = pods // len(NAMESPACES)
    saturation = "saturation-deployment-0"
    events: list[tuple[datetime, str]] = [
        (trial.start - timedelta(seconds=1), _step(trial.start - timedelta(seconds=1), 1,
                                                   "Starting measurements", "started")),
        (trial.start, _step(trial.start, 3, cl2.SATURATION_START[0], "started")),
    ]
    if trial.not_scheduled:
        events.append((trial.end, _timeout(trial.end, saturation, replicas,
                                           replicas - trial.not_scheduled)))
    else:
        for elapsed, share in FILL:
            when = trial.start + timedelta(seconds=trial.duration * elapsed)
            for namespace in NAMESPACES:
                summary = _pods(namespace, saturation, replicas, round(replicas * share), ": ")
                events.append((when, klog(when, f"WaitForControlledPodsRunning: {summary}",
                                          source="wait_for_pods.go:122")))
    events.append((trial.end, _step(trial.end, 4, cl2.SATURATION_END[0], "ended")))
    after = trial.end + timedelta(seconds=1)
    if trial.latency_timeouts:
        waited = after + timedelta(seconds=1)
        events.append((after, _step(after, 7, cl2.LATENCY_START[0], "started")))
        events += [(waited, _timeout(waited, f"latency-deployment-{index}", 1, 0))
                   for index in range(trial.latency_timeouts)]
        events.append((waited, _step(waited, 8, cl2.LATENCY_END[0], "ended")))
        after = waited + timedelta(seconds=1)
    failed = trial.not_scheduled or trial.latency_timeouts
    events += [
        (after, _step(after, 12, cl2.SATURATION_DELETION[0], "started")),
        # A multi-line message: only its first line carries a klog header.
        (after, klog(after, "SchedulingThroughput: {", source="simple_test_executor.go:97")
         + '  "perc50": 1\n}\n'),
        (after, klog(after, "  Status: " + ("Fail" if failed else "Success"),
                     source="clusterloader.go:256")),
    ]
    events.sort(key=lambda event: event[0])
    return "".join(text for _, text in events)


# ---------------------------------------------------------------------------
#  Scheduler runs (boards B1, B2, B3, K, P, ablation) and the Godel baseline
# ---------------------------------------------------------------------------

def write_trial(run_dir: str, trial: Trial, params: dict[str, Any],
                summary: dict[str, Any] | None = None) -> str:
    """One trial directory as run-experiment.sh leaves it, with the metric
    snapshots process-results.py and the occupancy analysis read."""

    pods = expected_pods(params)
    scheduled = pods - trial.not_scheduled
    trial_dir = os.path.join(run_dir, f"trial-{trial.number}")
    os.makedirs(trial_dir, exist_ok=True)
    with open(os.path.join(trial_dir, "cl2.log"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(cl2_log(trial, pods))
    start, end = int(trial.start.timestamp()), int(trial.end.timestamp())
    _dump(os.path.join(trial_dir, "timing.json"), {
        "trial": trial.number,
        "overall": {"start": start - 5, "end": end + 30, "duration": end - start + 35},
        "saturation": {"start": start, "end": end},
        "latency": {"start": None, "end": None},
    })
    metrics = os.path.join(trial_dir, "metrics-saturation")
    for name, delta in (("snap_all_candidates_failed", trial.acf),
                        ("snap_bind_conflict", trial.bind_conflicts),
                        ("snap_bind_success", scheduled)):
        _dump(os.path.join(metrics, f"{name}.json"),
              {"results": [{"metric": {}, "delta": delta}]})
    # The scheduler's own counter is only a fallback for the CL2 count, and it
    # is off by one here, so a test notices if the reduction falls back to it.
    _dump(os.path.join(metrics, "snap_scheduled.json"), {"results": [
        {"metric": {"profile": "para-scheduler"}, "delta": scheduled - 1},
        {"metric": {"profile": "default-scheduler"}, "delta": 5},
    ]})
    _dump(os.path.join(metrics, "snap_summary.json"),
          summary or {"binding": {"all_candidates_failed": trial.acf}})
    # The ACF event rate, constant over the phase, as a Prometheus range query.
    rate = trial.acf / trial.seconds
    first, last = start - 30, end + 30
    _dump(os.path.join(metrics, "all_candidates_failed_rate.json"), {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{
            "metric": {}, "values": [[t, str(rate)] for t in range(first, last + 1, 15)],
        }]},
    })
    write_overhead(metrics, trial.overhead, start, end)
    return trial_dir


def _per_pod(overhead: Overhead) -> dict[str, tuple[str, tuple[float, ...] | None]]:
    """Each resource metric file's pods and their values; the binder and the
    dispatcher run as one pod each."""

    def one(value):
        return None if value is None else (value,)

    return {
        "scheduler_cpu": ("para-scheduler", overhead.scheduler_cpu),
        "scheduler_memory_rss": ("para-scheduler", overhead.scheduler_rss),
        "binder_cpu": ("para-binder", one(overhead.binder_cpu)),
        "dispatcher_cpu": ("para-dispatcher", one(overhead.dispatcher_cpu)),
    }


def write_overhead(metrics: str, overhead: Overhead, start: int, end: int) -> None:
    """The saturation-phase files collect-metrics.sh writes for the overhead
    table: two histogram snapshots, and four resource range queries whose
    series change value at their last sample, the one the reduction reads."""

    for name, p99 in (("snap_algo_latency", overhead.algo_p99),
                      ("snap_e2e_latency", overhead.e2e_p99)):
        if p99 is not None:
            _dump(os.path.join(metrics, f"{name}.json"),
                  {"name": name, "duration": end - start, "count": 100, "sum": p99 * 50,
                   "avg": p99 / 2, "p50": p99 / 4, "p99": p99})
    for name, (component, values) in _per_pod(overhead).items():
        if values is not None:
            _dump(os.path.join(metrics, f"{name}.json"), {
                "status": "success",
                "data": {"resultType": "matrix", "result": [
                    {"metric": {"pod": f"{component}-{index}"},
                     "values": [[start, "0.001"], [end, str(value)]]}
                    for index, value in enumerate(values)]},
            })


def recorded_params(params: dict[str, Any]) -> dict[str, Any]:
    """Parameters as the runner records them: two of them as strings."""

    recorded = dict(params)
    for key in ("pods_per_node", "capacity_variance"):
        recorded[key] = str(params[key])
    return recorded


def write_run(results: str, directory: str, cell: dict[str, Any],
              trials: list[Trial]) -> str:
    """A recorded run of a registry cell, with its configuration."""

    run_dir = os.path.join(results, directory, f"{cell['cell']}_20260427_025104")
    _dump(os.path.join(run_dir, "config.json"), {
        "name": cell["cell"], "timestamp": "20260427_025104", "num_trials": len(trials),
        "parameters": recorded_params(cell["params"]),
    })
    for trial in trials:
        write_trial(run_dir, trial, cell["params"])
    return run_dir


def run_record(trial: Trial, params: dict[str, Any]) -> dict[str, Any]:
    """The archive record of a scheduler trial: throughput is scheduled pods
    over the saturation phase; the ACF rate is escalations over escalations
    plus scheduled pods; the bind-conflict rate is conflicts over conflicts
    plus successful binds, one per scheduled pod. Only the saturation phase's
    timeouts count: latency pods are not among the expected pods."""

    pods = expected_pods(params)
    scheduled = pods - trial.not_scheduled
    values = {
        "trial": trial.number,
        "expected_pods": pods,
        "scheduled_pods": scheduled,
        "scheduling_duration_s": round(trial.seconds, 3),
        "throughput_pods_per_s": round(scheduled / trial.seconds, 2),
        "acf_count": trial.acf,
        "acf_rate": round(trial.acf / (trial.acf + scheduled), 4),
        "bind_conflict_count": trial.bind_conflicts,
        "bind_conflict_rate": round(trial.bind_conflicts / (trial.bind_conflicts + scheduled), 4),
        "is_timeout": trial.not_scheduled > 0,
    }
    return {name: values[name] for name in schema.RUN_TRIAL_FIELDS}


def overhead_record(trial: Trial) -> dict[str, Any]:
    """The archive's overhead record of a trial: the P99s in milliseconds, and
    each component's last CPU and memory samples summed over its pods."""

    pods = {name: values for name, (_, values) in _per_pod(trial.overhead).items()}

    def milliseconds(seconds):
        return None if seconds is None else round(seconds * 1000, 3)

    def total(name):
        return None if pods[name] is None else round(sum(pods[name]), 6)

    memory = total("scheduler_memory_rss")
    values = {
        "trial": trial.number,
        "algo_p99_ms": milliseconds(trial.overhead.algo_p99),
        "e2e_p99_ms": milliseconds(trial.overhead.e2e_p99),
        "scheduler_cpu_total": total("scheduler_cpu"),
        "scheduler_mem_rss_total": None if memory is None else round(memory),
        "binder_cpu": total("binder_cpu"),
        "dispatcher_cpu": total("dispatcher_cpu"),
    }
    return {name: values[name] for name in schema.OVERHEAD_TRIAL_FIELDS}


@dataclass
class Collector:
    """What the Godel collector's summary reports for a trial."""

    failure: int  # all-reason bind failures
    conflict: int
    conflict_rate: float
    throughput: float

    def summary(self) -> dict[str, Any]:
        return {"binding": {"failure": self.failure, "conflict": self.conflict,
                            "conflict_rate": self.conflict_rate},
                "scheduling": {"throughput_pods_per_sec": self.throughput}}


def write_godel_run(results: str, directory: str, cell: dict[str, Any],
                    trials: list[tuple[Trial, Collector]]) -> str:
    """A recorded Godel baseline run: run-godel-baseline.sh's flat configuration."""

    name = cell["cell"][: -len("-godel")]
    params = cell["params"]
    run_dir = os.path.join(results, directory, f"20260512_145502-{name}-godel")
    _dump(os.path.join(run_dir, "config.json"), {
        "experiment_name": name, "baseline": "godel",
        "num_nodes": params["num_nodes"], "num_schedulers": params["num_schedulers"],
        "num_trials": len(trials), "pods_per_node": params["pods_per_node"],
        "cpu_request": params["cpu_request"], "memory_request": params["memory_request"],
        "variance": str(params["capacity_variance"]), "timestamp": "20260512_145502",
    })
    for trial, collector in trials:
        write_trial(run_dir, trial, params, summary=collector.summary())
    return run_dir


def godel_record(trial: Trial, collector: Collector, params: dict[str, Any]) -> dict[str, Any]:
    """Completion and throughput from the CL2 log, as for every arm; conflict
    counts from the collector, which the CL2 log does not carry."""

    run = run_record(trial, params)
    values = {
        **{key: run[key] for key in ("trial", "expected_pods", "scheduled_pods",
                                     "scheduling_duration_s", "throughput_pods_per_s",
                                     "is_timeout")},
        "acf_count": collector.failure,
        "acf_rate": collector.conflict_rate,
        "bind_conflict_count": collector.conflict,
        "bind_conflict_rate": collector.conflict_rate,
        "collector_throughput_pods_per_s": collector.throughput,
    }
    return {name: values[name] for name in schema.GODEL_TRIAL_FIELDS}


# ---------------------------------------------------------------------------
#  Placement quality (ablation) and occupancy intervals (B2)
# ---------------------------------------------------------------------------

def write_quality(trial_dir: str, mean: float, cumulative: tuple[int, int, int],
                  count: int) -> None:
    """quality.json as pull-quality-metrics.py writes it."""

    buckets = {f"{rank}.0": value for rank, value in enumerate(cumulative)}
    buckets["+Inf"] = count
    _dump(os.path.join(trial_dir, "metrics-saturation", "quality.json"), {
        "selected_node_score": {"mean": mean},
        "candidate_rank_accepted": {"count": count, "buckets": buckets},
    })


def occupancy_record(trial: Trial, params: dict[str, Any]) -> dict[str, Any]:
    """Per-interval ACF rate and useful throughput between the placement
    boundaries of 0, 80, 90 and 100%. The ACF event rate is constant, so each
    interval gets escalations in proportion to its length."""

    pods = expected_pods(params)
    replicas = pods // len(NAMESPACES)
    thresholds = [0, math.ceil(pods * 0.8), math.ceil(pods * 0.9), pods]
    # Boundaries as Unix times, as the analysis reads them from the log.
    boundaries = [trial.start.timestamp()]
    for threshold in thresholds[1:]:
        elapsed = next(e for e, share in FILL
                       if round(replicas * share) * len(NAMESPACES) >= threshold)
        boundaries.append((trial.start + timedelta(seconds=trial.duration * elapsed)).timestamp())
    intervals = []
    for index, name in enumerate(("0-80%", "80-90%", "90-100%")):
        length = boundaries[index + 1] - boundaries[index]
        placed = thresholds[index + 1] - thresholds[index]
        acf = trial.acf * length / (boundaries[-1] - boundaries[0])
        values = {"acf_rate_estimate": acf / (placed + acf),
                  "mean_useful_placement_throughput": placed / length}
        intervals.append({"interval": name,
                          **{metric: values[metric] for metric in schema.OCCUPANCY_METRICS}})
    return {"trial": trial.number, "intervals": intervals}


# ---------------------------------------------------------------------------
#  Data-plane rounds (board F)
# ---------------------------------------------------------------------------

@dataclass
class Round:
    """One module-F round and what its summaries report."""

    run_id: str
    round_dir: str
    cell: dict[str, Any]  # its registry cell
    order: int
    trial: int
    kept: bool
    throughput: float
    acf_rate: float
    t99_acf_rate: float
    censored: bool = False
    #: The binder's final counts, as the round summary records them.
    bind_success: int = 10_000
    acf_count: int = 120
    #: The window the round's collector recorded: start, end and snapshot end,
    #: in epoch seconds.
    window: tuple[int, int, int] = (1_000_000, 1_000_200, 1_000_230)


def write_round(anchored: str, item: Round, prometheus_url: str = "http://127.0.0.1:9") -> None:
    path = os.path.join(anchored, "module-f", item.run_id, item.round_dir)
    cell = item.cell
    _dump(os.path.join(path, "round-summary.json"), {
        "experiment": cell["experiment"], "order": item.order, "trial": item.trial,
        "method": cell["method"], "dataplane_profile": cell["profile"], "valid": item.kept,
        "control_plane": {"throughput_pods_per_s": item.throughput, "acf_rate": item.acf_rate,
                          "bind_success": item.bind_success, "acf_count": item.acf_count,
                          "candidate_k": cell["params"]["num_backup"]},
        "failure_injection": {"observed_failure_rate": 0.0098, "pass": True, "failed": 98,
                              "failed_semantics_valid": True},
    })
    start, end, snapshot_end = item.window
    _dump(os.path.join(path, "metrics-bind", "meta.json"), {
        "prometheus_url": prometheus_url,
        "time_range": {"start": str(start), "end": str(end), "step": "15s"},
        "snapshot_end": str(snapshot_end), "snap_end_pad_seconds": snapshot_end - end,
    })
    _dump(os.path.join(path, "completion-curve.json"), {
        "landmarks": {"tail_right_censored": item.censored, "q_bind_at_t99": 212.5,
                      "tail_fraction": 0.21},
    })
    params = {k: v for k, v in cell["params"].items() if k != "dataplane_profile"}
    _dump(os.path.join(path, "control-plane-source", "config.json"),
          {"name": f"{cell['cell']}-t{item.trial}", "parameters": recorded_params(params)})


def write_windowed_acf(anchored: str, rounds: list[Round]) -> None:
    with open(os.path.join(anchored, "acf-windowed.csv"), "w", newline="",
              encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["run_id", "round", "verified", "t99_acf_rate"])
        for item in rounds:
            writer.writerow([item.run_id, item.round_dir, "true", item.t99_acf_rate])


def round_record(item: Round) -> dict[str, Any]:
    cell = item.cell
    values = {
        "run_id": item.run_id, "round_dir": item.round_dir,
        "experiment": cell["experiment"], "order": item.order, "trial": item.trial,
        "method": cell["method"], "profile": cell["profile"], "kept": item.kept,
        "censored": item.censored, "q_bind_at_t99": 212.5,
        "throughput_raw_pods_per_s": item.throughput, "acf_rate": item.acf_rate,
        "tail_fraction": 0.21, "injection_observed": 0.0098, "injection_pass": True,
        "injection_failed": 98, "failed_semantics_valid": True,
        "t99_acf_rate": item.t99_acf_rate, "windowed_acf_verified": True,
    }
    return {name: values[name] for name in schema.ROUND_FIELDS}
