#!/usr/bin/env python3
"""Turn the committed archive into the per-figure JSON the plotters read.

This is the only place where measurements are selected and aggregated. Figure
scripts hold no numbers of their own: they read one JSON file and render it.
The overhead table is exported the same way, under the name `overhead`. The
split mirrors the simulation side, where `run.py` produces a cache, `verify.py`
checks it and `plot.py` only draws.

Input is the archive under `experiments/archive/`, which `reduce.py` produces
from recorded results, and the registry `experiments/registry.json`, which says
which series each archived cell belongs to. Neither the raw results nor any
machine-local path is needed, so every cluster figure except the trace figure
can be rebuilt from the repository alone.

Output is `experiments/work/figure-data/<figure>.json`, generated and not
version controlled. What is version controlled is a hash of each figure's `data`
block in `experiments/figures/figure-data.lock.json`; `--check` fails when an
export no longer matches it.

`--archive` reads another archive, such as one `reduce.py` built from new runs.
An archive of some boards only exports the figures those boards feed; a figure
that needs another board fails, naming it.

Usage:
    python export-figure-data.py --all
    python export-figure-data.py --figure robustness
    python export-figure-data.py --all --check        # what CI runs
    python export-figure-data.py --all --write-lock   # after an intended change
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from typing import Any, Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import data as envelope  # noqa: E402
from common import registry as reg  # noqa: E402
from common import schema  # noqa: E402
from common.trials import (  # noqa: E402
    FILTER_VERSION,
    as_float,
    filter_cell,
    median_iqr,
    median_iqr_of,
    median_of,
)

GENERATOR = "export-figure-data.py"
LOCK_FILE = os.path.join(_EXPERIMENTS, "figures", "figure-data.lock.json")
LOCK_SCHEMA = "parkour-figure-data-lock/v1"


class Archive(dict):
    """The archive's files, keyed as `schema.load` keys them, each read the
    first time an exporter asks for it.

    A figure can then be exported from an archive that holds only the boards it
    needs (`reduce.py --boards`); asking for a file the archive lacks fails,
    naming the boards that would have produced it.
    """

    def __init__(self, archive_dir: str) -> None:
        super().__init__()
        self.archive_dir = archive_dir

    def __missing__(self, key: str) -> dict[str, Any]:
        if key.startswith("boards/"):
            board = key.split("/", 1)[1]
            name, boards = schema.board_file(board), (board,)
        else:
            name, boards = schema.FILES[key], schema.GROUP_BOARDS[key]
        if not os.path.exists(os.path.join(self.archive_dir, name)):
            raise SystemExit(
                f"{self.archive_dir} holds no {name}: this figure needs board "
                f"{', '.join(boards)}, which the archive was not reduced for")
        self[key] = schema.read(self.archive_dir, name)
        return self[key]


class Inputs:
    """The archive and the registry, indexed by cell."""

    def __init__(self, archive_dir: str, registry_path: str) -> None:
        self.registry = reg.load(registry_path)
        self.archive = Archive(archive_dir)
        self.declared = reg.cell_index(self.registry)

    def cell(self, board: str, name: str) -> dict[str, Any]:
        try:
            return self.declared[(board, name)]
        except KeyError:
            raise SystemExit(f"archived cell {board}/{name} is not in the registry")

    def run_cells(self, board: str) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """(declared cell, trial rows) for every archived cell of a board."""

        key = "godel" if board == "godel" else f"boards/{board}"
        return [(self.cell(board, archived["cell"]),
                 [dict(trial, experiment=archived["cell"]) for trial in archived["trials"]])
                for archived in self.archive[key]["cells"]]

    def grouped(self, board: str, axis: str, figure: str | None = None
                ) -> dict[tuple[str, Any], list[dict[str, Any]]]:
        """Trial rows keyed by (arm, value of `axis`), optionally for one figure."""

        cells: dict[tuple[str, Any], list[dict[str, Any]]] = {}
        for declared, trials in self.run_cells(board):
            if figure is None or figure in declared["figures"]:
                cells[(declared["arm"], declared["params"][axis])] = trials
        return cells


def cell_median(trials: list[dict[str, Any]], field: str) -> float | None:
    """Median of `field` over the trials that survive the shared outlier filter."""

    return median_of(filter_cell(trials)[0], field)


def cell_band(trials: list[dict[str, Any]], field: str) -> dict[str, float | None]:
    """Median with the interquartile band, over the surviving trials."""

    stats = median_iqr_of(filter_cell(trials)[0], field)
    if stats is None:
        return {"median": None, "q1": None, "q3": None}
    median, q1, q3 = stats
    return {"median": median, "q1": q1, "q3": q3}


def _band(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "q1": None, "q3": None}
    median, q1, q3 = median_iqr(values)
    return {"median": median, "q1": q1, "q3": q3}


# ---------------------------------------------------------------------------
#  Board A: parameter sweeps
# ---------------------------------------------------------------------------

def _sweep(inputs: Inputs, board: str, axis: str, metric: str) -> dict[str, Any]:
    """Aggregate one parameter sweep into {x, series{arm: [values]}}.

    An arm is a (paradigm, sync pattern) pair; event-driven runs form a single
    arm because the pattern does not apply to them. The registry names them.
    """

    cells = {(arm, float(value)): trials
             for (arm, value), trials in inputs.grouped(board, axis).items()}
    axis_values = sorted({value for _, value in cells})
    series = {arm: [cell_median(cells.get((arm, value), []), metric)
                    for value in axis_values]
              for arm in sorted({arm for arm, _ in cells})}
    return {"x": axis_values, "series": series}


def export_robustness(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `robustness`: ACF against the fallback count, and against weight w.

    The x values are the recorded parameters. The paper counts the whole
    candidate list while the runner records fallbacks, so the `K = backup + 1`
    mapping is applied where the figure is drawn, not here.
    """

    return {
        "k_sweep": {"metric": "acf_rate", "axis": "num_backup",
                    **_sweep(inputs, "K", "num_backup", "acf_rate")},
        "w_sweep": {"metric": "acf_rate", "axis": "penalty_weight",
                    **_sweep(inputs, "P", "conflict_penalty", "acf_rate")},
    }


# ---------------------------------------------------------------------------
#  Board B: scale-out, with the Godel baseline
# ---------------------------------------------------------------------------

def _godel(inputs: Inputs, figure: str, axis: str) -> dict[Any, list[dict[str, Any]]]:
    """The Godel runs that feed `figure`, keyed by their value of `axis`."""

    return {value: trials for (_, value), trials
            in inputs.grouped("godel", axis, figure).items()}


def export_scalability_lowcontention(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `scalability-lowcontention`: throughput against cluster size.

    Board B1 at 1,000-5,000 nodes, plus the Godel baseline at the same sizes.
    Each arm reports the median with its interquartile band.
    """

    cells = inputs.grouped("B1", "num_nodes")
    node_counts = sorted({nodes for _, nodes in cells})
    godel = _godel(inputs, "scalability-lowcontention", "num_nodes")

    series: dict[str, Any] = {}
    for arm in sorted({arm for arm, _ in cells}):
        bands = [cell_band(cells.get((arm, nodes), []), "throughput_pods_per_s")
                 for nodes in node_counts]
        series[arm] = {stat: [band[stat] for band in bands]
                       for stat in ("median", "q1", "q3")}
    godel_bands = [cell_band(godel.get(nodes, []), "throughput_pods_per_s")
                   for nodes in node_counts]
    series["godel"] = {stat: [band[stat] for band in godel_bands]
                       for stat in ("median", "q1", "q3")}

    return {
        "x": node_counts,
        "axis": "num_nodes",
        "metric": "throughput_pods_per_s",
        "series": series,
    }


def export_scalability_schedulers(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `scalability-schedulers`: throughput and ACF against instance count.

    Board B3 at 2-10 scheduler instances, plus the Godel baseline. Both metrics
    travel together because the figure draws them on twinned axes.
    """

    cells = inputs.grouped("B3", "num_schedulers")
    counts = sorted({n for _, n in cells})
    godel = _godel(inputs, "scalability-schedulers", "num_schedulers")

    series: dict[str, Any] = {}
    for arm in sorted({arm for arm, _ in cells}):
        series[arm] = {
            "throughput": [cell_median(cells.get((arm, n), []), "throughput_pods_per_s")
                           for n in counts],
            "acf": [cell_median(cells.get((arm, n), []), "acf_rate") for n in counts],
        }
    series["godel"] = {
        "throughput": [cell_median(godel.get(n, []), "throughput_pods_per_s") for n in counts],
        "acf": [cell_median(godel.get(n, []), "acf_rate") for n in counts],
    }

    return {
        "x": counts,
        "axis": "num_schedulers",
        "metrics": ["throughput_pods_per_s", "acf_rate"],
        "series": series,
    }


def _point(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """One Pareto point: throughput and ACF, each as median with its IQR."""

    kept = filter_cell(trials)[0]
    point: dict[str, Any] = {"n": len(kept)}
    for name, field in (("throughput", "throughput_pods_per_s"), ("acf", "acf_rate")):
        stats = median_iqr_of(kept, field)
        if stats is None:
            point[name] = {"median": None, "q1": None, "q3": None}
        else:
            median, q1, q3 = stats
            point[name] = {"median": median, "q1": q1, "q3": q3}
    return point


def export_pareto_all_scales(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `pareto-all-scales`: the throughput/ACF trade-off at four sizes.

    Board B2 at 2,000-20,000 nodes, plus the Godel baseline. Every arm reports
    both metrics with their interquartile ranges, because the figure draws each
    point with error bars on both axes.
    """

    cells = inputs.grouped("B2", "num_nodes")
    node_counts = sorted({nodes for _, nodes in cells})
    godel = _godel(inputs, "pareto-all-scales", "num_nodes")

    series: dict[str, Any] = {}
    for arm in sorted({arm for arm, _ in cells}):
        series[arm] = [_point(cells.get((arm, nodes), [])) for nodes in node_counts]
    series["godel"] = [_point(godel.get(nodes, [])) for nodes in node_counts]

    return {
        "x": node_counts,
        "axis": "num_nodes",
        "metrics": ["throughput_pods_per_s", "acf_rate"],
        "series": series,
    }


# ---------------------------------------------------------------------------
#  Board C: ablation
# ---------------------------------------------------------------------------

#: Ablation configurations in the order the figures draw them, as declared.
ABLATION_CONFIGS = [arm for arm, _, _ in reg.ABLATION]
ABLATION_PARADIGMS = ("event", "periodic")


def _ablation_cells(inputs: Inputs) -> dict[tuple[str, str], str]:
    """(paradigm, configuration) -> ablation cell name."""

    return {(cell["paradigm"], cell["arm"]): cell["cell"]
            for cell in reg.cells(inputs.registry, board="ablation")}


def export_ablation_quality_a(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `ablation-quality-a`: conflict and throughput per mechanism.

    Board C, four configurations in each paradigm. Two conflict measures travel
    together because the figure contrasts them: ACF is a pod escalated to a full
    reschedule, while the bind-conflict rate counts candidate-level failures the
    binder absorbed. They coincide when K=0 and diverge once a fallback list
    exists, which is the point of the panel.
    """

    names = _ablation_cells(inputs)
    trials_of = {declared["cell"]: trials
                 for declared, trials in inputs.run_cells("ablation")}

    series: dict[str, Any] = {}
    for paradigm in ABLATION_PARADIGMS:
        acf, bind, throughput = [], [], []
        for config in ABLATION_CONFIGS:
            kept = filter_cell(trials_of.get(names[(paradigm, config)], []))[0]
            acf.append(_band([v for v in (as_float(t.get("acf_rate")) for t in kept)
                              if v is not None]))
            bind.append(_band([v for v in (as_float(t.get("bind_conflict_rate"))
                                           for t in kept) if v is not None]))
            throughput.append(median_of(kept, "throughput_pods_per_s"))
        series[paradigm] = {"acf": acf, "bind_conflict": bind, "throughput": throughput}

    return {
        "configs": ABLATION_CONFIGS,
        "metrics": ["acf_rate", "bind_conflict_rate", "throughput_pods_per_s"],
        "series": series,
    }


def _rank_distribution(total: float | None,
                       cumulative: list[float | None]) -> dict[str, Any] | None:
    """Recover exact rank 0/1/2 shares from the cumulative histogram counts."""

    if total is None or total <= 0 or any(edge is None for edge in cumulative):
        return None
    edges = [float(edge) for edge in cumulative]
    counts = [edges[0], max(0.0, edges[1] - edges[0]), max(0.0, edges[2] - edges[1])]
    fractions = [count / float(total) for count in counts]
    return {
        "rank_fractions": {str(rank): fraction
                           for rank, fraction in enumerate(fractions)},
        "cumulative_fractions": {
            "0": fractions[0],
            "1": fractions[0] + fractions[1],
        },
    }


def _quality_cell(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one ablation cell's per-trial scheduling-quality records.

    Deliberately not routed through the trial outlier filter: these records carry
    no duration or completion counters, so the filter's tests do not apply.
    Every trial that recorded them contributes.
    """

    scores: list[float] = []
    rank_gt0: list[float] = []
    rank_shares: dict[str, list[float]] = {str(rank): [] for rank in range(3)}
    cumulative: dict[str, list[float]] = {str(rank): [] for rank in range(2)}

    for trial in trials:
        if trial.get("selected_node_score_mean") is not None:
            scores.append(float(trial["selected_node_score_mean"]))

        distribution = _rank_distribution(trial.get("candidate_rank_count"),
                                          trial.get("candidate_rank_cumulative") or [None])
        if distribution is None:
            continue
        rank_gt0.append(
            distribution["rank_fractions"]["1"] + distribution["rank_fractions"]["2"]
        )
        for rank in range(3):
            rank_shares[str(rank)].append(distribution["rank_fractions"][str(rank)])
        for rank in range(2):
            cumulative[str(rank)].append(distribution["cumulative_fractions"][str(rank)])

    return {
        "score": _band(scores),
        "rank_gt0": _band(rank_gt0),
        "rank_shares": {rank: _band(values) for rank, values in rank_shares.items()},
        "cumulative": {rank: _band(values) for rank, values in cumulative.items()},
    }


def export_ablation_quality_bc(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `ablation-quality-bc`: placement quality per mechanism.

    Board C again, but from the per-trial quality records rather than the run
    records: the mean framework score of the accepted candidate, and how often
    the accepted candidate was the top-ranked one.
    """

    names = _ablation_cells(inputs)
    quality = {cell["cell"]: cell["trials"] for cell in inputs.archive["quality"]["cells"]}
    series = {paradigm: [_quality_cell(quality[names[(paradigm, config)]])
                         for config in ABLATION_CONFIGS]
              for paradigm in ABLATION_PARADIGMS}
    return {
        "configs": ABLATION_CONFIGS,
        "metrics": ["selected_node_score", "candidate_rank_accepted"],
        "series": series,
    }


# ---------------------------------------------------------------------------
#  Board B2 again: occupancy intervals
# ---------------------------------------------------------------------------

def export_occupancy_intervals(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `occupancy-intervals-1col`: metrics bucketed by workload fill.

    Board B2, through the occupancy analysis rather than the run records,
    because the figure's x axis is how full the cluster was rather than how
    large it is. Each interval reports the median and quartiles over the trials.
    """

    occupancy = inputs.archive["occupancy"]
    metadata = occupancy["metadata"]
    if metadata.get("scope") != "b2":
        raise SystemExit("the archived occupancy analysis is not a B2 analysis")

    scenarios: dict[str, Any] = {}
    for key, scenario in occupancy["scenarios"].items():
        configs = {}
        for label, block in scenario["configs"].items():
            summary = []
            for position, interval in enumerate(metadata["intervals"]):
                row: dict[str, Any] = {"interval": interval}
                for metric in schema.OCCUPANCY_METRICS:
                    values = []
                    for trial in block["trials"]:
                        entry = trial["intervals"][position]
                        if entry["interval"] != interval:
                            raise SystemExit(f"{key}/{label}: interval order differs")
                        values.append(float(entry[metric]))
                    row[metric] = {**_band(values), "n": len(values)}
                summary.append(row)
            configs[label] = {"cell": block["cell"], "summary": summary}
        scenarios[key] = {"configs": configs}
    return {"metadata": metadata, "scenarios": scenarios}


# ---------------------------------------------------------------------------
#  Board B2 again: the overhead table
# ---------------------------------------------------------------------------

#: The overhead table's measured fields; `trial` is what joins them to the runs.
OVERHEAD_FIELDS = schema.OVERHEAD_TRIAL_FIELDS[1:]
OVERHEAD_UNITS = {
    "algo_p99_ms": "ms", "e2e_p99_ms": "ms", "scheduler_cpu_total": "cores",
    "scheduler_mem_rss_total": "bytes", "binder_cpu": "cores", "dispatcher_cpu": "cores",
    "throughput_pods_per_s": "pods/s", "pods_per_core_s": "pods/(core*s)",
}


def export_overhead(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Table `overhead`: scheduler latency and resource use.

    The trials that count are those the shared outlier filter keeps from the
    same cells' run records, so the table rests on the trials the figures of
    those cells use. Each row reports the medians over the kept trials, and the
    CPU efficiency: median throughput over median scheduler CPU, in scheduled
    pods per second per core.
    """

    runs = {}
    for board in boards_of(inputs.registry, "overhead"):
        for declared, trials in inputs.run_cells(board):
            runs[declared["cell"]] = (declared, trials)
    rows = []
    for archived in inputs.archive["overhead"]["cells"]:
        declared, trials = runs[archived["cell"]]
        records = {record["trial"]: record for record in archived["trials"]}
        kept = filter_cell(trials)[0]
        missing = [trial["trial"] for trial in kept if trial["trial"] not in records]
        if missing:
            raise SystemExit(f"overhead/{archived['cell']}: no record of trials {missing}")
        measured = [records[trial["trial"]] for trial in kept]
        row: dict[str, Any] = {"cell": archived["cell"], "arm": declared["arm"],
                               "paradigm": declared["paradigm"],
                               "num_nodes": declared["params"]["num_nodes"], "n": len(kept)}
        for field in OVERHEAD_FIELDS:
            row[field] = median_of(measured, field)
        throughput = median_of(kept, "throughput_pods_per_s")
        cpu = row["scheduler_cpu_total"]
        row["throughput_pods_per_s"] = throughput
        row["pods_per_core_s"] = throughput / cpu if throughput and cpu else None
        rows.append(row)
    rows.sort(key=lambda row: (row["num_nodes"], row["cell"]))
    return {"units": OVERHEAD_UNITS, "rows": rows}


# ---------------------------------------------------------------------------
#  Board F: data-plane latency injection
#
#  Rounds carry their own keep and censor flags, which the matrix gate applied
#  when the campaign ran, so the trial outlier filter is deliberately not used.
# ---------------------------------------------------------------------------

#: Archive profile name -> figure x-axis label.
F_PROFILES = {"Z0": "none", "Dreal": "delay", "Dreal-F1": "delay+fail"}
#: Throughput and ACF definitions; `t99` is the uniform completion cut point
#: the campaign prescribes, and the two metrics share it.
F_THROUGHPUT_COLUMNS = {"t99": "q_bind_at_t99", "raw": "throughput_raw_pods_per_s"}
F_ACF_COLUMNS = {"t99": "t99_acf_rate", "whole": "acf_rate"}


def export_dataplane_sensitivity(inputs: Inputs, options: argparse.Namespace) -> dict[str, Any]:
    """Figure `dataplane-sensitivity`: behaviour under injected pod-startup delay.

    Folds the anchored campaign's kept rounds into the twelve plotted cells. The
    startup-failure gate numbers travel with the data so the figure stays
    auditable from its own input.
    """

    throughput_column = F_THROUGHPUT_COLUMNS[options.throughput_metric]
    acf_column = F_ACF_COLUMNS[options.acf_metric]
    declared = {(cell["experiment"], cell["method"], cell["profile"]): cell
                for cell in reg.cells(inputs.registry, board="F")}

    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    expected: dict[tuple[str, int, str], int] = {}
    audit: list[dict[str, Any]] = []
    for row in inputs.archive["dataplane"]["rounds"]:
        if row["experiment"].startswith("F2"):
            audit.append({
                "matrix": row["experiment"],
                "order": str(row["order"]),
                "method": row["method"],
                "kept": bool(row["kept"]),
                "failure_rate": float(row["injection_observed"]),
                "gate_pass": bool(row["injection_pass"]),
                "semantics_ok": row["injection_failed"] == row["failed_semantics_valid"],
            })
        if not row["kept"]:
            continue
        cell = declared.get((row["experiment"], row["method"], row["profile"]))
        if cell is None:
            raise SystemExit(f"kept round {row['run_id']}/{row['round_dir']} "
                             "matches no registry cell")
        acf_value = row[acf_column]
        if acf_value is None:
            raise SystemExit(f"no verified windowed ACF for {row['run_id']}/"
                             f"{row['round_dir']}; pass --acf-metric whole")

        key = (cell["paradigm"], cell["params"]["num_backup"], F_PROFILES[row["profile"]])
        expected[key] = cell["trials"]
        grouped[key].append({
            "trial": int(row["trial"]),
            "method": row["method"],
            "source": row["experiment"],
            "throughput": float(row[throughput_column]),
            "acf": float(acf_value),
            "acf_whole": float(row["acf_rate"]),
            "tail_fraction": row["tail_fraction"],
            "censored": bool(row["censored"]),
        })

    cells = []
    for (paradigm, k, profile), records in sorted(grouped.items()):
        if len(records) != expected[(paradigm, k, profile)]:
            raise SystemExit(
                f"cell {(paradigm, k, profile)} has {len(records)} kept rounds, "
                f"the registry declares {expected[(paradigm, k, profile)]}")
        records.sort(key=lambda record: record["trial"])
        throughput = [record["throughput"] for record in records]
        acf = [record["acf"] for record in records]
        uncensored = [record["throughput"] for record in records if not record["censored"]]
        tails = [float(record["tail_fraction"]) for record in records
                 if record["tail_fraction"] is not None]
        cells.append({
            "paradigm": paradigm,
            "k": k,
            "profile": profile,
            "tput": throughput,
            "tput_mean": statistics.mean(throughput),
            "tput_min": min(throughput),
            "tput_max": max(throughput),
            "acf": acf,
            "acf_mean": statistics.mean(acf),
            "acf_min": min(acf),
            "acf_max": max(acf),
            "n": len(records),
            "censored": sum(1 for record in records if record["censored"]),
            "tput_mean_uncensored": statistics.mean(uncensored) if uncensored else None,
            "acf_whole_mean": statistics.mean(record["acf_whole"] for record in records),
            "tail_fraction": statistics.mean(tails) if tails else None,
            "method": records[0]["method"],
            "source": records[0]["source"],
        })

    return {
        "throughput_metric": options.throughput_metric,
        "acf_metric": options.acf_metric,
        "cells": cells,
        "audit": audit,
    }


#: figure -> (exporter, whether it applies the trial outlier filter)
EXPORTERS: dict[str, tuple[Callable[..., Any], bool]] = {
    "ablation-quality-a": (export_ablation_quality_a, True),
    "ablation-quality-bc": (export_ablation_quality_bc, False),
    "dataplane-sensitivity": (export_dataplane_sensitivity, False),
    "occupancy-intervals-1col": (export_occupancy_intervals, False),
    "overhead": (export_overhead, True),
    "pareto-all-scales": (export_pareto_all_scales, True),
    "robustness": (export_robustness, True),
    "scalability-lowcontention": (export_scalability_lowcontention, True),
    "scalability-schedulers": (export_scalability_schedulers, True),
}


def boards_of(registry: dict[str, Any], figure: str) -> list[str]:
    """The boards whose cells feed `figure`, as the registry declares them."""

    return sorted({cell["board"] for cell in reg.cells(registry, figure=figure)})


def read_lock() -> dict[str, str]:
    if not os.path.exists(LOCK_FILE):
        return {}
    with open(LOCK_FILE, encoding="utf-8") as handle:
        lock = json.load(handle)
    if lock.get("schema") != LOCK_SCHEMA:
        raise SystemExit(f"{LOCK_FILE}: unexpected schema {lock.get('schema')!r}")
    return lock["figures"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", default=schema.ARCHIVE_DIR,
                        help="archive directory (default: experiments/archive)")
    parser.add_argument("--registry", default=reg.REGISTRY_FILE,
                        help="registry (default: experiments/registry.json)")
    parser.add_argument("--figure", action="append", choices=sorted(EXPORTERS),
                        help="figure to export; repeatable")
    parser.add_argument("--all", action="store_true", help="export every figure")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="also print the exported payload")
    parser.add_argument("--check", action="store_true",
                        help="fail unless each figure's data matches the lock file")
    parser.add_argument("--write-lock", action="store_true",
                        help="record each figure's data hash in the lock file")

    board_f = parser.add_argument_group("board F", "Throughput and ACF definitions.")
    board_f.add_argument("--throughput-metric", default="t99",
                         choices=sorted(F_THROUGHPUT_COLUMNS),
                         help="'t99' is the uniform cut point the campaign prescribes")
    board_f.add_argument("--acf-metric", default="t99", choices=sorted(F_ACF_COLUMNS),
                         help="'t99' shares the throughput cut point")
    args = parser.parse_args(argv)

    if args.all:
        figures = sorted(EXPORTERS)
    elif args.figure:
        figures = args.figure
    else:
        parser.error("pass --figure NAME or --all")
    if (args.check or args.write_lock) and (args.throughput_metric, args.acf_metric) != ("t99", "t99"):
        parser.error("the lock file records the default board F metrics only")

    inputs = Inputs(args.archive, args.registry)
    hashes: dict[str, str] = {}
    for figure in figures:
        build, filtered = EXPORTERS[figure]
        payload = build(inputs, args)
        hashes[figure] = envelope.data_sha256(payload)
        path = envelope.write(
            envelope.path_for(_EXPERIMENTS, figure),
            figure=figure,
            source="cluster",
            data=payload,
            generator=GENERATOR,
            boards=boards_of(inputs.registry, figure),
            filter_version=FILTER_VERSION if filtered else None,
        )
        print(f"wrote {path}")
        if args.show:
            print(json.dumps(payload, indent=2, sort_keys=True))

    if args.write_lock:
        lock = {**read_lock(), **hashes}
        envelope.write_text_atomic(LOCK_FILE, envelope.canonical_json(
            {"schema": LOCK_SCHEMA, "figures": dict(sorted(lock.items()))}) + "\n")
        print(f"wrote {LOCK_FILE}")
    if args.check:
        lock = read_lock()
        stale = [f for f in figures if lock.get(f) != hashes[f]]
        for figure in stale:
            print(f"figure data differs from the lock: {figure}", file=sys.stderr)
        if stale:
            return 1
        print(f"figure data matches the lock ({len(figures)} figures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
